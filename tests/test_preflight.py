"""Tests for tools/preflight.py (the read-only setup preflight). Run: python3 tests/test_preflight.py

The preflight is run as a real subprocess inside a COPY of the project whose folder name contains spaces and brackets, with a scratch HOME, a controlled PATH,
stand-in environments and tiny stand-in model files. The isolation guard is active inside the preflight too, so any attempt to open a USB device,
reach the outside network or touch production data would abort it. Nothing here uses the live server, data, the Stick or the network.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import stick_config as sc  # noqa: E402
from test_config import free_port, project, write_conf  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class patched:
    """Temporarily replace attributes (restored afterwards: the modules patched here are shared with the rest of the test process)."""
    def __init__(self, *changes):
        self.changes = changes                   # (object, name, value)

    def __enter__(self):
        self.saved = [(obj, name, getattr(obj, name)) for obj, name, _ in self.changes]
        for obj, name, value in self.changes:
            setattr(obj, name, value)

    def __exit__(self, *exc):
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)


def load_preflight():
    spec = importlib.util.spec_from_file_location("preflight_under_test", ROOT / "tools" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(*folders):
    """Every file and folder under the given roots with size and modification time: proves a run changed nothing."""
    seen = {}
    for folder in folders:
        for path in sorted(Path(folder).rglob("*")):
            stat = path.stat()
            seen[str(path)] = (stat.st_size, stat.st_mtime_ns, path.is_dir())
    return seen


def ready_project(tmp, port=None, with_claude=True, ffmpeg=True, packages_ok=True, models=True, whisper=True):
    """A project copy with everything the preflight requires, using stand-ins: scripts that print package lists, tiny model files and a tiny manifest."""
    root = project(tmp)
    (root / "requirements" / "server.txt").write_text("numpy==2.5.5\nscipy==1.18.1\n")
    (root / "requirements" / "tts.txt").write_text("numpy==2.5.5\nphonikud @ git+https://example.invalid/phonikud.git@abc\n")
    (root / "requirements" / "stick-tools.txt").write_text(f"{sc.TOOL_PROGRAM}==1.29.0\n")
    stand_ins = {"voice-env": '{"py":"3.14.3","pkgs":{"numpy":"%s","scipy":"1.18.1"}}' % ("2.5.5" if packages_ok else "9.9.9"),
                 "tts-env312": '{"py":"3.12.16","pkgs":{"numpy":"2.5.5","phonikud":"0.4.1"}}',
                 "tools-env": '{"py":"3.14.3","pkgs":{"%s":"1.29.0"}}' % sc.TOOL_PROGRAM}
    for env, output in stand_ins.items():
        python = root / env / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(f"#!/bin/sh\necho '{output}'\n")
        python.chmod(0o755)
    tts_files = {"a.onnx": b"voice-model-bytes", "b.onnx": b"phonemizer-bytes", "c.json": b'{"config": true}'}
    weights = b"whisper-weights"
    manifest = {"schema": 1,
                "tts": {"source": "stand-in", "revision": "r" * 40, "folder": "models_dir",
                        "files": [{"name": n, "bytes": len(b), "sha256": sha(b)} for n, b in tts_files.items()]},
                "whisper": {"source": "stand-in", "cache_folder": "models--stand-in--whisper", "observed_revision": "a" * 40, "pinned_in_code": False,
                            "files": [{"name": "weights.safetensors", "bytes": len(weights), "sha256": sha(weights)}]},
                "environments_approx_bytes": {"voice_env": 1000, "tts_env": 100, "tools_env": 10}}
    (root / "requirements" / "models.json").write_text(json.dumps(manifest))
    if models:
        (root / "tts-models").mkdir()
        for name, data in tts_files.items():
            (root / "tts-models" / name).write_bytes(data)
    hf = Path(tmp) / "hf-cache"
    hf.mkdir()
    if whisper:
        snap = hf / "models--stand-in--whisper" / "snapshots" / ("a" * 40)
        snap.mkdir(parents=True)
        (snap / "weights.safetensors").write_bytes(weights)
    bindir = Path(tmp) / "fake bin"
    bindir.mkdir()
    marker = Path(tmp) / "claude-was-run"
    if with_claude:
        (bindir / "claude").write_text(f'#!/bin/sh\ntouch "{marker}"\n')          # if the preflight ever RAN this, the marker would appear
        (bindir / "claude").chmod(0o755)
    if ffmpeg:
        (bindir / "ffmpeg").write_text("#!/bin/sh\nexit 0\n")
        (bindir / "ffmpeg").chmod(0o755)
    (Path(tmp) / "home").mkdir(exist_ok=True)
    port = port or free_port()
    write_conf(root, f"[server]\nadvertise_host = 192.0.2.10\nbind_host = 127.0.0.1\ndevice_port = {port}\npanel_port = {free_port()}\n")
    return root, bindir, marker, hf


def run_preflight(root, tmp, bindir, *args, extra_env=None, hf=None):
    python_dir = str(Path(shutil.which("python3")).parent)
    environment = {"HOME": str(Path(tmp) / "home"), "PATH": f"{bindir}:{python_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
                   "HUGGINGFACE_HUB_CACHE": str(hf or Path(tmp) / "hf-cache"), "PYTHONPATH": isolation_guard.child_pythonpath(), "STICK_TEST_GUARD": "1"}
    environment.update(extra_env or {})
    return subprocess.run([sys.executable, str(root / "tools" / "preflight.py"), *args], capture_output=True, text=True, env=environment, cwd=tmp, timeout=120)


def checks(done):
    return json.loads(done.stdout)["checks"]


def find(done, title_start):
    for c in checks(done):
        if c["title"].startswith(title_start):
            return c
    raise AssertionError(f"no check starting with {title_start!r} in {[c['title'] for c in checks(done)]}")


# ---- a ready machine, and what it must NOT do ----------------------------------------------------------------------------------

@case
def a_ready_installation_passes_from_a_folder_with_spaces_and_leaves_no_trace():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        before = snapshot(root, Path(tmp) / "home", hf, bindir)
        done = run_preflight(root, tmp, bindir, "--json", "--no-usb")
        assert done.returncode == 0, (done.stdout[-1500:], done.stderr)
        report = json.loads(done.stdout)
        assert report["schema"] == 1 and report["read_only"] is True and report["summary"]["ok"] is True
        assert " " in report["project"] and "(" in report["project"]
        for title in ("Server environment", "Hebrew TTS environment", "Stick tools environment", "Hebrew TTS model files", "Whisper speech model", "Host configuration"):
            assert find(done, title)["status"] == "ok", find(done, title)
        assert not marker.exists()                                                      # the Claude CLI was found, never run
        assert snapshot(root, Path(tmp) / "home", hf, bindir) == before                 # nothing created, changed or removed
        assert not list(root.rglob("__pycache__")) and not (root / "data").exists() and not (root / "build").exists()
        assert not (root / "secrets").exists() and not (root / "recordings").exists()


@case
def the_text_report_separates_required_from_optional_and_lists_what_it_did_not_check():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        done = run_preflight(root, tmp, bindir, "--no-usb")
        text = done.stdout
        for heading in ("REQUIRED to run the assistant", "SETUP only", "OPTIONAL (connectors, hooks, shortcuts, device selection)", "DEVELOPMENT tools", "NOT CHECKED"):
            assert heading in text, heading
        for item in ("Stick firmware version", "Claude Code login", "Claude Code hooks", "End-to-end connectivity", "firewall"):
            assert item in text, item
        assert "This was read-only" in text and "Result: nothing required is missing." in text
        assert text.index("REQUIRED") < text.index("OPTIONAL") < text.index("NOT CHECKED")


@case
def usb_devices_are_listed_without_opening_them_even_with_the_guard_watching():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        done = run_preflight(root, tmp, bindir, "--json")                              # USB listing ON; the guard inside would abort any serial open
        assert done.returncode == 0 and "isolation violation" not in done.stderr, (done.stderr[-800:])
        assert find(done, "USB devices")["status"] == "info"


# ---- missing and invalid things -----------------------------------------------------------------------------------------------------

@case
def missing_configuration_is_a_required_failure_with_the_fix_and_still_creates_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        (root / "config" / "host.conf").unlink()
        before = snapshot(root, Path(tmp) / "home")
        done = run_preflight(root, tmp, bindir, "--no-usb")
        assert done.returncode == 1
        assert "FAIL" in done.stdout and "advertise_host" in done.stdout and "host.example.conf" in done.stdout and "init --advertise-host" in done.stdout
        assert snapshot(root, Path(tmp) / "home") == before and not (root / "config" / "host.conf").exists()


@case
def missing_environments_packages_models_and_tools_are_reported_individually():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp, packages_ok=False, models=False, whisper=False)
        shutil.rmtree(root / "tts-env312")
        done = run_preflight(root, tmp, bindir, "--json", "--no-usb")
        assert done.returncode == 1
        assert find(done, "Server environment")["status"] == "warn" and "differ" in find(done, "Server environment")["detail"]    # present but another version: untested
        tts_env = find(done, "Hebrew TTS environment")
        assert tts_env["status"] == "fail" and "not found" in tts_env["detail"] and "setup-mac.sh" in tts_env["fix"]
        assert find(done, "Hebrew TTS model files")["status"] == "fail" and "a.onnx missing" in find(done, "Hebrew TTS model files")["detail"]
        whisper = find(done, "Whisper speech model")
        assert whisper["status"] == "warn" and "downloaded automatically" in whisper["detail"]                                  # not required to be present beforehand
        assert find(done, "Disk and memory")["detail"].count("need about") == 1
        assert {c["group"] for c in checks(done) if c["status"] == "fail"} == {"required"}


@case
def missing_tools_are_required_failures_with_what_to_do_and_optional_tools_are_only_notes():
    pre = load_preflight()
    with patched((pre.shutil, "which", lambda name, path=None: None)):                 # nothing is installed
        report = pre.Report()
        pre.check_executables(report, None)
    by_title = {c["title"]: c for c in report.checks}
    assert by_title["Claude Code CLI"]["status"] == "fail" and by_title["Claude Code CLI"]["group"] == "required" and "log in" in by_title["Claude Code CLI"]["fix"]
    assert by_title["ffmpeg"]["status"] == "fail" and "brew install ffmpeg" in by_title["ffmpeg"]["fix"]
    git = [c for c in report.checks if c["title"].startswith("git")][0]
    assert git["group"] == "setup" and git["status"] == "warn" and "Command Line Tools" in git["fix"]
    for c in report.checks:
        if c["group"] in ("optional", "development"):
            assert c["status"] == "info", c                                            # a missing optional or development tool never fails the preflight
    assert {c["title"] for c in report.required_problems()} == {"Claude Code CLI", "ffmpeg"}


@case
def a_package_missing_from_an_environment_fails_with_the_install_command():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        (root / "requirements" / "server.txt").write_text("numpy==2.5.5\nscipy==1.18.1\nmlx-whisper==0.4.3\n")
        done = run_preflight(root, tmp, bindir, "--json", "--no-usb")
        check = find(done, "Server environment")
        assert done.returncode == 1 and check["status"] == "fail" and "mlx-whisper" in check["detail"] and "pip install -r requirements/server.txt" in check["fix"]


@case
def model_files_are_checked_by_size_and_by_checksum_only_when_asked():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        (root / "tts-models" / "a.onnx").write_bytes(b"voice-model-bytez")                  # same length, different content
        quick = run_preflight(root, tmp, bindir, "--json", "--no-usb")
        assert find(quick, "Hebrew TTS model files")["status"] == "ok" and "--hashes" in find(quick, "Hebrew TTS model files")["detail"]
        strict = run_preflight(root, tmp, bindir, "--json", "--no-usb", "--hashes")
        assert find(strict, "Hebrew TTS model files")["status"] == "fail" and "hash differs" in find(strict, "Hebrew TTS model files")["detail"]
        (root / "tts-models" / "a.onnx").write_bytes(b"short")
        assert "unexpected size" in find(run_preflight(root, tmp, bindir, "--json", "--no-usb"), "Hebrew TTS model files")["detail"]


@case
def locations_given_by_environment_variables_reach_the_process_and_are_reported_with_their_source():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        elsewhere = Path(tmp) / "installed elsewhere (old)"
        elsewhere.mkdir()
        moved = {}
        for name, variable in (("voice-env", "STICK_VOICE_ENV"), ("tts-env312", "STICK_TTS_ENV")):
            shutil.move(str(root / name), str(elsewhere / name))
            moved[variable] = str(elsewhere / name)
        without = run_preflight(root, tmp, bindir, "--json", "--no-usb")                  # the variables never arrived (the "assigned but not exported" mistake)
        assert without.returncode == 1 and find(without, "Server environment")["status"] == "fail"
        done = run_preflight(root, tmp, bindir, "--json", "--no-usb", extra_env=moved)
        assert done.returncode == 0, done.stdout[-1500:]
        where = find(done, "Locations in effect")["detail"]
        assert f"voice_env={moved['STICK_VOICE_ENV']} [env]" in where and f"tts_env={moved['STICK_TTS_ENV']} [env]" in where, where
        assert "tools_env=" in where and "[default]" in where                              # what was not set is labelled as derived from the folder


@case
def a_pinned_local_whisper_copy_made_by_the_setup_is_checked_in_place_of_the_hugging_face_cache():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp, whisper=False)                    # no cache copy at all
        folder = root / "whisper-model"
        folder.mkdir()
        (folder / "config.json").write_text("{}")
        (folder / "weights.safetensors").write_bytes(b"whisper-weights")
        done = run_preflight(root, tmp, bindir, "--json", "--no-usb", "--hashes")
        check = find(done, "Whisper speech model")
        assert check["status"] == "ok" and "pinned local copy" in check["detail"] and "checksum matches" in check["detail"], check
        (folder / "weights.safetensors").write_bytes(b"damaged")
        done = run_preflight(root, tmp, bindir, "--json", "--no-usb")
        check = find(done, "Whisper speech model")
        assert done.returncode == 1 and check["status"] == "fail" and "setup_mac.py" in check["fix"], check


@case
def alternate_ports_are_reported_and_a_busy_port_is_flagged_not_failed():
    with tempfile.TemporaryDirectory() as tmp:
        port = free_port()
        root, bindir, marker, hf = ready_project(tmp, port=port)
        listener = socket.socket()
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        try:
            done = run_preflight(root, tmp, bindir, "--json", "--no-usb")
        finally:
            listener.close()
        assert done.returncode == 0
        assert f":{port}" in find(done, "Addresses and ports")["detail"] and "loopback only" in find(done, "Addresses and ports")["detail"]
        busy = find(done, f"Port {port}")
        assert busy["status"] == "warn" and "already in use" in busy["detail"] and busy["group"] == "info"


@case
def an_address_that_is_not_this_macs_is_a_warning_with_the_command_to_find_the_right_one():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        write_conf(root, f"[server]\nadvertise_host = 192.0.2.10\nbind_host = 192.0.2.10\ndevice_port = {free_port()}\n")
        done = run_preflight(root, tmp, bindir, "--json", "--no-usb")
        warn = find(done, "Advertised address")
        assert warn["status"] == "warn" and "suggest-address" in warn["fix"] and done.returncode == 0


# ---- platform labelling and no invented requirements --------------------------------------------------------------------------------

@case
def untested_platforms_are_labelled_untested_and_this_machines_specifications_are_not_turned_into_minimums():
    pre = load_preflight()
    import platform as platform_module
    for mac, chip, expect in (("27.2.1", "Apple M5 Pro", "ok"), ("15.1", "Apple M2", "untested"), ("27.0", "Apple M9 Max", "untested")):
        with patched((platform_module, "system", lambda: "Darwin"), (platform_module, "machine", lambda: "arm64"),
                     (platform_module, "mac_ver", lambda mac=mac: (mac, ("", "", ""), "")), (pre, "run", lambda command, timeout=20, chip=chip: (0, chip + "\n"))):
            report = pre.Report()
            pre.check_platform(report)
        entry = [c for c in report.checks if c["title"] == "Tested platform"][0]
        assert entry["status"] == expect, (mac, chip, entry)
        if expect == "untested":
            assert "UNTESTED" in entry["detail"] and "not a minimum requirement" in entry["detail"]
    with patched((platform_module, "system", lambda: "Darwin"), (platform_module, "machine", lambda: "x86_64"), (pre, "run", lambda command, timeout=20: (0, "1\n"))):
        report = pre.Report()
        pre.check_platform(report)                                                   # Rosetta
    assert report.checks[0]["status"] == "fail" and "Rosetta" in report.checks[0]["detail"]
    with patched((platform_module, "system", lambda: "Linux")):
        report = pre.Report()
        pre.check_platform(report)
    assert report.checks[0]["status"] == "fail"
    # resources: free disk and RAM are reported; only the size of what is actually missing can trigger a warning
    with patched((pre.shutil, "disk_usage", lambda path: type("U", (), {"free": 10})())):
        report = pre.Report()
        pre.check_resources(report, 0)
        assert report.checks[0]["status"] == "ok"
        report = pre.Report()
        pre.check_resources(report, 1000)
        assert report.checks[0]["status"] == "warn"


@case
def usb_metadata_is_listed_without_serial_numbers_and_never_claims_to_identify_the_device():
    pre = load_preflight()
    sample = (Path(__file__).resolve().parent / "fixtures" / "usb_profile_sample.json").read_text()
    entry = json.loads(sample)["SPUSBDataType"][0]["_items"][0]
    with patched((pre, "run", lambda command, timeout=20: (0, sample)), (pre.stick_config, "usb_candidates", lambda: [f"/dev/{sc.USB_PREFIX}TEST"])):
        nodes, devices = pre.usb_metadata()
        assert devices == [{"name": entry["_name"], "vendor_id": "0x303a", "product_id": "0x1001"}]    # the keyboard is not listed; no serial number anywhere
        assert "SECRET" not in json.dumps(devices)
        report = pre.Report()
        pre.check_usb(report, None)
    text = report.checks[0]["detail"]
    assert "does NOT identify a StickS3 or its firmware" in text and "SECRET" not in text


# ---- network probes are opt-in and bounded --------------------------------------------------------------------------------------------

@case
def network_probes_are_off_by_default_bounded_and_send_nothing():
    pre = load_preflight()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    received = []

    def accept():
        server.settimeout(3)
        try:
            conn, _ = server.accept()
            conn.settimeout(0.5)
            try:
                received.append(conn.recv(100))
            except OSError:
                received.append(b"")
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=accept)
    thread.start()
    started = time.time()
    assert pre.tcp_probe("127.0.0.1", port, timeout=1.0) == (True, "")
    thread.join()
    assert received == [b""]                                                           # a connection was opened; not one byte was sent
    closed = free_port()
    ok, why = pre.tcp_probe("127.0.0.1", closed, timeout=0.3)
    assert ok is False and why and time.time() - started < 5
    assert pre.PROBE_TIMEOUT_S <= 3.0 and len(pre.NETWORK_TARGETS) <= 5               # at most 5 x 3 s even if every target hangs
    server.close()
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        done = run_preflight(root, tmp, bindir, "--json", "--no-usb")                  # default: no probe lines at all (and the guard would abort an outside connect)
        assert done.returncode == 0 and not [c for c in checks(done) if c["title"].startswith(("Network:", "Probe:"))]


@case
def probing_the_server_only_opens_a_connection_and_creates_no_job():
    with tempfile.TemporaryDirectory() as tmp:
        port = free_port()
        root, bindir, marker, hf = ready_project(tmp, port=port)
        listener = socket.socket()
        listener.bind(("127.0.0.1", port))
        listener.listen(2)
        got = []

        def accept():
            listener.settimeout(20)
            try:
                conn, _ = listener.accept()
                conn.settimeout(1.0)
                try:
                    got.append(conn.recv(100))
                except OSError:
                    got.append(b"")
                conn.close()
            except OSError:
                pass

        thread = threading.Thread(target=accept)
        thread.start()
        try:
            done = run_preflight(root, tmp, bindir, "--json", "--no-usb", "--probe-server")
            thread.join(timeout=25)
        finally:
            listener.close()
        probe = find(done, "Probe: device port")
        assert "accepted a TCP connection (no request was sent)" in probe["detail"] and got == [b""]
        assert "nothing is listening" in find(done, "Probe: panel")["detail"]


@case
def an_unusable_command_line_is_refused_cleanly():
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir, marker, hf = ready_project(tmp)
        done = run_preflight(root, tmp, bindir, "--install")
        assert done.returncode == 2 and "usage" in done.stderr.lower()                  # there is no option that installs or changes anything
        import ast
        source = (ROOT / "tools" / "preflight.py").read_text()
        imported = {a.name.split(".")[0] for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom) and n.module}
        assert imported - {"stick_config"} <= set(sys.stdlib_module_names), imported       # standard library only: no pyserial, no network or database client
        for forbidden in ("pip install -r", "urlretrieve", "urlopen", "sqlite3", "os.makedirs", ".mkdir(", ".write_text(", "-p\""):
            assert forbidden not in source.replace("Install: {python} -m pip install -r requirements/{pin_file}", ""), forbidden


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:700])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:500])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
