#!/usr/bin/env python3
"""Read-only setup preflight for the StickS3 voice assistant (docs/preflight.md).

    python3 tools/preflight.py [--json] [--hashes] [--no-usb] [--network] [--probe-server]

It looks at this Mac and this installation and says what is ready, what is missing and what it did NOT check. It never installs or downloads anything, never
creates or changes application state (no database, no settings, no config file, no Python bytecode), never starts the server, never runs Claude, never reads
account data or private recordings, and never opens a serial connection (USB devices are only listed). Network probes are opt-in and bounded.

Exit status: 0 = nothing required is missing, 1 = at least one required item is missing or invalid, 2 = bad command line.
"""
import sys

sys.dont_write_bytecode = True          # a preflight must not even leave .pyc files behind

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stick_config  # noqa: E402

SCHEMA = 1
# The only combination this project has ever been run on. It is evidence about what was TESTED, not a statement of minimum requirements.
TESTED = {"os": "macOS 27.2", "chip": "Apple M5 Pro", "python_server": "3.14", "python_tts": "3.12"}
NETWORK_TARGETS = [("pypi.org", 443), ("github.com", 443), ("huggingface.co", 443)]     # installs use these; --network only opens a TCP connection
PROBE_TIMEOUT_S = 3.0
NOT_CHECKED = [
    "Stick firmware version and identity (the Stick is never contacted; only USB device metadata is listed)",
    "Whether the Stick has Wi-Fi saved, its battery, or whether it runs this software",
    "Claude Code login, plan, usage limits and connectors (account data is never read; the CLI is not run)",
    "Claude Code hooks installed in your global settings, and desktop shortcuts",
    "End-to-end connectivity: no request is sent to the assistant or to the Stick (unless --probe-server, which only opens a TCP connection)",
    "macOS firewall prompts, the Mac's sleep settings, and the router's address assignment",
    "Licence terms of models and packages (see THIRD_PARTY_NOTICES.md; the Hebrew voice is non-commercial)",
]


class Report:
    def __init__(self):
        self.checks = []

    def add(self, group, status, title, detail="", fix=""):
        self.checks.append({"group": group, "status": status, "title": title, "detail": detail, "fix": fix})

    def required_problems(self):
        return [c for c in self.checks if c["group"] == "required" and c["status"] == "fail"]


def run(command, timeout=20):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


# ---- checks -------------------------------------------------------------------------------------------------------------------------

def check_platform(report):
    system, machine = platform.system(), platform.machine()
    mac = platform.mac_ver()[0] if system == "Darwin" else ""
    chip = ""
    if system == "Darwin":
        _, out = run(["sysctl", "-n", "machdep.cpu.brand_string"], 5)
        chip = out.strip()
    translated = False
    if system == "Darwin" and machine != "arm64":
        _, out = run(["sysctl", "-in", "sysctl.proc_translated"], 5)
        translated = out.strip() == "1"
    if system != "Darwin":
        report.add("required", "fail", "Operating system", f"{system} {machine}: this project targets macOS on Apple silicon", "Use an Apple silicon Mac.")
    elif machine != "arm64":
        why = " (this Python runs under Rosetta; use a native arm64 Python)" if translated else ""
        report.add("required", "fail", "Apple silicon", f"{machine}{why}", "The speech model (MLX) needs Apple silicon and a native arm64 Python.")
    else:
        report.add("required", "ok", "Apple silicon", f"arm64, macOS {mac}" + (f", {chip}" if chip else ""))
    tested = mac.startswith(TESTED["os"].split()[-1].rsplit(".", 1)[0]) and TESTED["chip"] in chip
    if system == "Darwin" and machine == "arm64":
        report.add("info", "ok" if tested else "untested", "Tested platform",
                   "same OS family and chip as the development machine" if tested else
                   f"this combination (macOS {mac}, {chip or 'unknown chip'}) is UNTESTED; the project has only run on {TESTED['os']} / {TESTED['chip']}. "
                   "That is a record of what was tested, not a minimum requirement.")


def check_resources(report, needed_bytes):
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        ram = None
    usage = shutil.disk_usage(ROOT)
    detail = f"{human_bytes(usage.free)} free where the project lives" + (f"; {human_bytes(ram)} RAM" if ram else "")
    if needed_bytes:
        detail += f"; components not present yet need about {human_bytes(needed_bytes)}"
    status = "warn" if needed_bytes and usage.free < needed_bytes else "ok"
    report.add("info", status, "Disk and memory", detail,
               "Free some disk space before installing the missing components." if status == "warn" else "")


def local_addresses():
    return set(stick_config.local_ipv4_addresses())


def lsof_listener(port):
    code, out = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fcn"], 10)
    names = sorted({line[1:] for line in out.splitlines() if line.startswith("c")})
    return names


def check_config(report):
    try:
        cfg = stick_config.load()
    except stick_config.ConfigError as error:
        report.add("required", "fail", "Host configuration", str(error).splitlines()[0],
                   "See docs/configuration.md. " + ("Create config/host.conf." if not (ROOT / stick_config.CONFIG_RELATIVE).exists() else "Fix config/host.conf."))
        report.config_message = str(error)
        try:                                        # still report locations (the Stick tools and the environment checks need them)
            return stick_config.load(require_server=False)
        except stick_config.ConfigError:
            return None
    where = cfg.config_file or "environment variables and defaults only (no config file)"
    report.add("required", "ok", "Host configuration", f"valid ({where})")
    detail = (f"Stick connects to {cfg.advertise_host}:{cfg.device_port}; server listens on {cfg.bind_host}:{cfg.device_port}; "
              f"panel on 127.0.0.1:{cfg.panel_port} (loopback only)")
    report.add("required", "ok", "Addresses and ports", detail)
    shown = cfg.as_dict()
    report.add("info", "ok", "Locations in effect (and where each value came from)",
               "; ".join(f"{key}={shown[key]} [{cfg.sources.get(key)}]" for key in ("voice_env", "tts_env", "tools_env", "models_dir", "data_dir", "secrets_dir", "recordings_dir")
                         if shown.get(key) is not None) + "; advertise_host " + f"[{cfg.sources.get('advertise_host')}]")
    mine = local_addresses()
    if cfg.advertise_host and re.match(r"^\d+\.\d+\.\d+\.\d+$", cfg.advertise_host) and mine and cfg.advertise_host not in mine:
        report.add("required", "warn", "Advertised address", f"{cfg.advertise_host} is not an address of this Mac right now (Wi-Fi off or a different network?)",
                   "Check advertise_host with: python3 stick_config.py suggest-address")
    if cfg.bind_host not in (None, "0.0.0.0") and mine and cfg.bind_host not in mine and not cfg.bind_host.startswith("127."):
        report.add("required", "warn", "Listen address", f"{cfg.bind_host} is not an address of this Mac right now; the server could not listen on it")
    for label, port in (("device port", cfg.device_port), ("panel port", cfg.panel_port)):
        users = lsof_listener(port)
        if users:
            report.add("info", "warn", f"Port {port} ({label})", "already in use by: " + ", ".join(users) + " (this is expected if the assistant is already running)")
    return cfg


def which(name, configured=None):
    if configured and "/" in configured:
        return configured if os.access(configured, os.X_OK) and os.path.isfile(configured) else None
    return shutil.which(configured or name, path=os.environ.get("PATH", "") + os.pathsep + "/opt/homebrew/bin" + os.pathsep +
                        os.path.expanduser("~/.npm-global/bin") + os.pathsep + os.path.expanduser("~/.local/bin"))


def check_executables(report, cfg):
    claude = which("claude", cfg.claude if cfg else None)
    report.add("required", "ok" if claude else "fail", "Claude Code CLI", claude or "not found",
               "" if claude else "Install Claude Code, then log in (not checked here). Put its folder on PATH or set `claude` in the configuration.")
    ffmpeg = which("ffmpeg", cfg.ffmpeg if cfg else None)
    report.add("required", "ok" if ffmpeg else "fail", "ffmpeg", ffmpeg or "not found", "" if ffmpeg else "Install it with Homebrew: brew install ffmpeg")
    git = which("git")
    report.add("setup", "ok" if git else "warn", "git (needed once to install the Hebrew phonemizer from GitHub)", git or "not found",
               "" if git else "Install the Xcode Command Line Tools (xcode-select --install).")
    for version in ("3.14", "3.12"):
        found = which(f"python{version}")
        report.add("setup", "ok" if found else "info", f"python{version} (only needed to create a missing environment)", found or "not found")
    for name, note in (("brew", "Homebrew (optional; the usual way to install ffmpeg and Python)"), ("node", "Node.js (development tools and one way to install the Claude CLI)")):
        found = which(name)
        report.add("optional" if name == "brew" else "development", "ok" if found else "info", note, found or "not found")


def parse_pins(path):
    pins = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=@ ]", line, maxsplit=1)[0]
        match = re.search(r"==([^\s;]+)", line)
        pins[name.lower().replace("_", "-")] = match.group(1) if match else None       # None: a direct URL pin (presence only)
    return pins


def installed_packages(python):
    code = ("import json, importlib.metadata as m, sys; "
            "print(json.dumps({'py': '%d.%d.%d' % sys.version_info[:3], "
            "'pkgs': {d.metadata['Name'].lower().replace('_', '-'): d.version for d in m.distributions() if d.metadata['Name']}}))")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        done = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=60, env=env)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def check_environments(report, cfg, manifest):
    needed = 0
    plan = [("voice_env", "Server environment (voice-env)", "server.txt", cfg.voice_python if cfg else None),
            ("tts_env", "Hebrew TTS environment", "tts.txt", cfg.tts_python if cfg else None),
            ("tools_env", "Stick tools environment (mpremote)", "stick-tools.txt", (cfg.tools_env / "bin" / "python") if cfg else None)]
    for key, title, pin_file, python in plan:
        if python is None:
            report.add("required", "fail", title, "location unknown because the configuration could not be read")
            continue
        folder = python.parent.parent
        if not python.exists():
            needed += manifest.get("environments_approx_bytes", {}).get(key, 0)
            report.add("required", "fail", title, f"not found at {folder}", "Create it with ./setup-mac.sh (docs/mac-setup.md), or set its location in the configuration.")
            continue
        data = installed_packages(python)
        pins = parse_pins(ROOT / "requirements" / pin_file)
        if data is None:
            report.add("required", "fail", title, f"found at {folder}, but its Python could not be run", "Recreate the environment.")
            continue
        missing = sorted(n for n in pins if n not in data["pkgs"])
        different = sorted(n for n, v in pins.items() if v and n in data["pkgs"] and data["pkgs"][n] != v)
        if missing:
            report.add("required", "fail", title, f"Python {data['py']}; {len(missing)} of {len(pins)} pinned packages missing (e.g. {', '.join(missing[:4])})",
                       f"Install: {python} -m pip install -r requirements/{pin_file}")
        elif different:
            report.add("required", "warn", title, f"Python {data['py']}; all packages present but {len(different)} differ from the pinned versions (e.g. {', '.join(different[:3])}) - untested combination")
        else:
            report.add("required", "ok", title, f"Python {data['py']}; all {len(pins)} pinned packages present at the pinned versions")
    return needed


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def hf_hub_dir():
    for name in ("HUGGINGFACE_HUB_CACHE",):
        if os.environ.get(name):
            return Path(os.environ[name])
    home = os.environ.get("HF_HOME")
    return (Path(home) / "hub") if home else Path.home() / ".cache" / "huggingface" / "hub"


def check_models(report, cfg, manifest, verify_hashes):
    needed = 0
    tts = manifest["tts"]
    folder = cfg.models_dir if cfg else None
    if folder is None:
        report.add("required", "fail", "Hebrew TTS model files", "location unknown because the configuration could not be read")
    else:
        problems, ok = [], 0
        for entry in tts["files"]:
            path = folder / entry["name"]
            if not path.is_file():
                problems.append(f"{entry['name']} missing")
                needed += entry["bytes"]
            elif path.stat().st_size != entry["bytes"]:
                problems.append(f"{entry['name']} has an unexpected size")
            elif verify_hashes and sha256_of(path) != entry["sha256"]:
                problems.append(f"{entry['name']} hash differs from the pinned one")
            else:
                ok += 1
        how = "sizes and hashes" if verify_hashes else "sizes (use --hashes to verify checksums)"
        if problems:
            report.add("required", "fail", "Hebrew TTS model files", "; ".join(problems),
                       f"Download the pinned files (revision {tts['revision'][:8]}); ./setup-mac.sh downloads and checks them (docs/mac-setup.md). Licence terms: THIRD_PARTY_NOTICES.md.")
        else:
            report.add("required", "ok", "Hebrew TTS model files", f"{ok} of {len(tts['files'])} present at {folder}; {how} match")
    whisper = manifest["whisper"]
    local = cfg.whisper_dir if cfg else None
    if local and (local / "config.json").is_file() and (local / "weights.safetensors").is_file():      # the pinned copy made by tools/setup_mac.py (used by the server first)
        weights = whisper["files"][0]
        size_ok = (local / "weights.safetensors").stat().st_size == weights["bytes"]
        hash_ok = (sha256_of(local / "weights.safetensors") == weights["sha256"]) if verify_hashes and size_ok else None
        detail = f"pinned local copy at {local}; " + ("size matches" if size_ok else "size DIFFERS from the pinned file")
        detail += "" if hash_ok is None else ("; checksum matches" if hash_ok else "; checksum DIFFERS")
        report.add("required", "ok" if size_ok and hash_ok is not False else "fail", "Whisper speech model", detail,
                   "" if size_ok and hash_ok is not False else "Re-run tools/setup_mac.py apply (it re-downloads what does not verify).")
        return needed
    snapshots = hf_hub_dir() / whisper["cache_folder"] / "snapshots"
    found = None
    if snapshots.is_dir():
        for snap in sorted(snapshots.iterdir()):
            if (snap / "weights.safetensors").exists():
                found = snap
    if not found:
        needed += whisper["files"][0]["bytes"]
        report.add("required", "warn", "Whisper speech model", "not in the Hugging Face cache; it is downloaded automatically on the first question (needs network)",
                   "Optional: start the server once while online to fetch it now.")
    else:
        weights = whisper["files"][0]
        size_ok = (found / "weights.safetensors").stat().st_size == weights["bytes"]
        hash_ok = (sha256_of(found / "weights.safetensors") == weights["sha256"]) if verify_hashes and size_ok else None
        pinned = found.name == whisper["observed_revision"]
        detail = f"revision {found.name[:8]} found"
        detail += "; size matches the one observed on the development machine" if size_ok else "; size differs from the one observed on the development machine"
        if hash_ok is not None:
            detail += "; checksum matches" if hash_ok else "; checksum differs"
        detail += "" if pinned else "; a different revision than the development machine used (the code does not pin one)"
        report.add("required", "ok" if size_ok and hash_ok is not False else "warn", "Whisper speech model", detail)
    return needed


def usb_metadata():
    nodes = stick_config.usb_candidates()
    devices = []
    code, out = run(["system_profiler", "SPUSBDataType", "-json"], 30)
    try:
        tree = json.loads(out) if code == 0 else {}
    except ValueError:
        tree = {}

    def walk(items):
        for item in items:
            vendor = str(item.get("vendor_id", ""))
            name = str(item.get("_name", ""))
            if "303a" in vendor.lower() or "usb jtag" in name.lower() or "espressif" in str(item.get("manufacturer", "")).lower():
                devices.append({"name": name, "vendor_id": vendor.split()[0] if vendor else "", "product_id": str(item.get("product_id", ""))})
            walk(item.get("_items", []))
    walk(tree.get("SPUSBDataType", []))
    return nodes, devices


def check_usb(report, cfg):
    nodes, devices = usb_metadata()
    if nodes:
        detail = "serial devices present: " + ", ".join(nodes)
    else:
        detail = "no /dev/cu.usbmodem* device present (connect the Stick with a data cable when you want to use the Stick tools)"
    if devices:
        detail += "; USB metadata: " + "; ".join(f"{d['name']} (vendor {d['vendor_id']}, product {d['product_id']})" for d in devices) + \
            " - this only suggests an ESP32-class device; it does NOT identify a StickS3 or its firmware"
    report.add("optional", "info", "USB devices (listed only; nothing is opened)", detail)
    if cfg and cfg.usb_port and not os.path.exists(cfg.usb_port):
        report.add("optional", "warn", "Configured usb_port", f"{cfg.usb_port} is not present now")
    if len(nodes) > 1 and cfg and not cfg.usb_port:
        report.add("optional", "warn", "USB selection", "several candidates and no usb_port set: the Stick tools will ask you to choose", "Set usb_port in the configuration.")


def check_project_files(report):
    for name in ("Start Voice Assistant.command", "Voice Assistant Panel.command"):
        path = ROOT / name
        ok = path.is_file() and os.access(path, os.X_OK)
        report.add("optional", "ok" if ok else "warn", f"Launcher: {name}", "present and executable" if ok else "missing or not executable",
                   "" if ok else f"chmod +x '{path}'")


def tcp_probe(host, port, timeout=PROBE_TIMEOUT_S):
    """(True, '') if a TCP connection could be opened within `timeout` seconds. Nothing is sent. Bounded by `timeout`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as error:
        return False, error.__class__.__name__


def check_network(report, cfg, probe_network, probe_server):
    if probe_network:
        for host, port in NETWORK_TARGETS:
            ok, why = tcp_probe(host, port)
            report.add("optional", "ok" if ok else "warn", f"Network: {host}", "reachable (TCP connect only)" if ok else f"not reachable ({why})",
                       "" if ok else "Installing packages and models needs this host.")
    if probe_server and cfg and cfg.advertise_host:
        for label, host, port in (("device port", cfg.connect_host, cfg.device_port), ("panel", "127.0.0.1", cfg.panel_port)):
            ok, why = tcp_probe(host, port)
            report.add("optional", "info", f"Probe: {label} {host}:{port}", "something accepted a TCP connection (no request was sent)" if ok else f"nothing is listening ({why})")


# ---- output -----------------------------------------------------------------------------------------------------------------------------

LABELS = {"ok": "ok     ", "warn": "WARN   ", "fail": "FAIL   ", "info": "info   ", "untested": "UNTEST ", "skipped": "skipped"}
GROUPS = [("required", "REQUIRED to run the assistant"), ("setup", "SETUP only (needed to install, not to run)"),
          ("optional", "OPTIONAL (connectors, hooks, shortcuts, device selection)"), ("development", "DEVELOPMENT tools"), ("info", "NOTES")]


def render_text(report):
    lines = [f"StickS3 voice assistant: setup preflight (read-only)   project: {ROOT}", ""]
    for group, heading in GROUPS:
        items = [c for c in report.checks if c["group"] == group]
        if not items:
            continue
        lines.append(heading)
        for c in items:
            lines.append(f"  {LABELS[c['status']]} {c['title']}" + (f": {c['detail']}" if c["detail"] else ""))
            if c["fix"] and c["status"] in ("fail", "warn"):
                lines.append(f"          -> {c['fix']}")
        lines.append("")
    lines.append("NOT CHECKED (this tool never does these)")
    lines += [f"  - {item}" for item in NOT_CHECKED]
    lines.append("")
    problems = report.required_problems()
    lines.append("Result: " + (f"{len(problems)} required item(s) need attention (see FAIL lines above)." if problems else "nothing required is missing."))
    lines.append("This was read-only: nothing was installed, changed, started or sent.")
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(description="Read-only setup preflight (docs/preflight.md)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--hashes", action="store_true", help="also verify model checksums (reads about 2 GB; takes a few seconds)")
    parser.add_argument("--no-usb", action="store_true", help="skip listing USB devices")
    parser.add_argument("--network", action="store_true", help="opt in: open a TCP connection to PyPI, GitHub and Hugging Face (3 s limit each; nothing is sent)")
    parser.add_argument("--probe-server", action="store_true", help="opt in: open a TCP connection to the configured ports (nothing is sent; no job is created)")
    args = parser.parse_args(argv)

    manifest = json.loads((ROOT / "requirements" / "models.json").read_text())
    report = Report()
    report.config_message = ""
    check_platform(report)
    cfg = check_config(report)
    check_executables(report, cfg)
    needed = check_environments(report, cfg, manifest)
    needed += check_models(report, cfg, manifest, args.hashes)
    check_resources(report, needed)
    check_project_files(report)
    if not args.no_usb:
        check_usb(report, cfg)
    check_network(report, cfg, args.network, args.probe_server)
    if args.json:
        problems = report.required_problems()
        print(json.dumps({"schema": SCHEMA, "read_only": True, "project": str(ROOT), "tested_platform": TESTED, "checks": report.checks,
                          "not_checked": NOT_CHECKED, "summary": {"required_problems": len(problems), "ok": not problems}}, indent=2))
    else:
        print(render_text(report))
        if report.config_message:
            print("\n" + report.config_message)
    return 1 if report.required_problems() else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
