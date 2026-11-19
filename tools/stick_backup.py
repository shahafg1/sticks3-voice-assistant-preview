#!/usr/bin/env python3
"""Identify a StickS3 and make a private, verified FILE backup of its /flash (docs/stick-backup.md).

    python3 tools/stick_backup.py ports                                   list connected candidates (lists /dev only; opens nothing)
    python3 tools/stick_backup.py identify --port /dev/cu.usbmodemXXXX    what is on the Stick (model, firmware, boot option, app)
    python3 tools/stick_backup.py backup   --port /dev/cu.usbmodemXXXX    identify, then copy /flash (minus the device key) and verify it
          [--expect-device ID]   refuse unless the Stick reports this device id (from an earlier identify)
          [--allow-unsupported]  back up a Stick whose firmware is identified but is not the supported version
          [--restart]            restart the Stick when finished, so the assistant runs again

WHAT CONNECTING DOES TO THE STICK (identify and backup alike):
  * Every command goes through the Stick tool (mpremote) on the port you name, and starts with Ctrl-C: this STOPS the running
    assistant app and leaves the Stick at the MicroPython REPL (the raw REPL while a command runs).
  * "resume" is used, so MicroPython's soft reset is skipped, and this tool never resets the Stick unless --restart is given.
    Whether opening a USB serial port resets an ESP32-S3 depends on the USB stack; on the development unit it did not, but that is
    not a guarantee for other Macs, cables or firmware.
  * Until the Stick is restarted (--restart, or the restart command this tool prints), the assistant does not listen, remind or
    notify. Unplugging may not restart it: the Stick has a battery.
  * Nothing is written to /flash or to the settings area (NVS), but that does not mean "no impact": the assistant is stopped.

WHAT THE BACKUP IS, AND IS NOT:
  * A copy of the files under /flash, except the device key, each checked by size and SHA-256 against the device.
  * NOT a firmware image, NOT the NVS area (Wi-Fi credentials, boot_option and other UIFlow settings live there), NOT the bootloader
    or partition table, and NOT the device key, which is deliberately left out (a new key can be provisioned). It cannot bring back a
    Stick whose firmware is damaged or replaced, nor anything stored outside /flash.
  * A complete backup does not prove recovery works. This tool never restores; tools/stick_restore.py does, and it has been tested
    only against a simulated Stick.

The backup contains the Stick's settings cache and other files that may be personal: it is kept private (folders 0700, files 0600)
under the configured data folder, never printed, uploaded or added to git. Supported firmware: docs/stick-backup.md.
Tested only against a SIMULATED Stick (tests/test_stick_backup.py); the device-side snippets have not run on real hardware yet.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stick_config  # noqa: E402

TOOL_VERSION = 1
# The only firmware this kit has been validated on (one unit). Anything else is reported as unsupported or unknown, never as "probably fine".
SUPPORTED = {"model": "StickS3", "micropython": "1.27.0", "build_date": "2026-06-26", "uiflow": "V2.4.8"}
KEY_FILE = "/flash/device_key.txt"
EXCLUDED = {KEY_FILE: "the device key (a secret): never copied or hashed; it can be provisioned again with tools/stick_setup.py key"}
KEY_LIKE_REASON = "named like the device key (for example a partial copy left by an interrupted provisioning): never copied or hashed"


def key_like(path):
    """The device key, or anything named like it in any folder (a partial copy, another spelling): never copied, hashed or restored."""
    return path == KEY_FILE or path.rsplit("/", 1)[-1].lower().startswith("device_key")
LIMITS = [
    "File backup of /flash only: not a firmware image, not the NVS settings area (Wi-Fi credentials, boot_option), not the bootloader or partition table.",
    "The device key is excluded on purpose and cannot be restored from this backup; anything stored outside /flash is not included.",
    "A complete backup does not prove that recovery works. Restoring (tools/stick_restore.py) has been tested only against a simulated Stick.",
    "Treat every file here as potentially sensitive (settings cache, configuration): keep it private, do not share or commit it.",
]
IMPACT = ("Connecting stops the running assistant app and leaves the Stick at the MicroPython REPL. MicroPython's soft reset is skipped "
          "(resume) and the Stick is not reset by this tool unless --restart is given; until it restarts, the assistant does not "
          "listen, remind or notify. Nothing is written to /flash or NVS.")
DEV_DIR = os.environ.get("STICK_DEV_DIR", "/dev")          # tests point this at a folder of simulated device nodes
EXEC_TIMEOUT_S, COPY_TIMEOUT_S = 60, 120
SPACE_MARGIN = 1.2
SPACE_EXTRA = 1024 * 1024

# ---- device-side snippets (MicroPython). They print only markers with names, sizes, flags and hashes: never file contents or Wi-Fi values.
IDENTIFY_SNIPPET = r'''
import os, json
r = {}
try:
    u = os.uname()
    for k in ("machine", "release", "version"):
        v = getattr(u, k, None)
        if v:
            r[k] = v
except Exception:
    pass
try:
    import esp32
    r["uiflow"] = esp32.firmware_info()[3]
except Exception:
    pass
try:
    import esp32
    n = esp32.NVS("uiflow")
    try:
        r["boot_option"] = n.get_u8("boot_option")
    except OSError as e:
        r["boot_option"] = "not set" if e.args and e.args[0] in (4354, -4354) else None
    for key, name in (("ssid0", "wifi_ssid_saved"), ("pswd0", "wifi_password_saved")):
        try:
            v = n.get_str(key)
            r[name] = bool(v)
        except OSError as e:
            r[name] = False if e.args and e.args[0] in (4354, -4354) else None
        except Exception:
            r[name] = None
        v = None
except Exception:
    pass
try:
    import machine, hashlib, binascii
    r["id"] = binascii.hexlify(hashlib.sha256(machine.unique_id()).digest()).decode()[:12]
except Exception:
    pass
try:
    os.stat("/flash/device_key.txt")
    r["key_present"] = True
except OSError:
    r["key_present"] = False
print("STICKID:" + json.dumps(r))
'''

LIST_SNIPPET = r'''
import os, json
def walk(p):
    for e in os.ilistdir(p):
        full = p + "/" + e[0]
        if e[1] & 0x4000:
            print("STICKLS:" + json.dumps([full, "d", 0]))
            walk(full)
        else:
            print("STICKLS:" + json.dumps([full, "f", e[3] if len(e) > 3 else os.stat(full)[6]]))
walk("/flash")
print("STICKLS-END")
'''

HASH_SNIPPET = r'''
import hashlib, binascii, json
for p in PATHS:
    try:
        h = hashlib.sha256()
        n = 0
        with open(p, "rb") as f:
            while True:
                b = f.read(1024)
                if not b:
                    break
                n += len(b)
                h.update(b)
        print("STICKHASH:" + json.dumps([p, binascii.hexlify(h.digest()).decode(), n]))
    except OSError:
        print("STICKHASH:" + json.dumps([p, None, None]))
print("STICKHASH-END")
'''


class Refused(Exception):
    """Stopped before contacting the Stick (exit code 2)."""


class Failed(Exception):
    """The Stick was contacted, but the request could not be completed (exit code 1)."""


# ---- the Stick tool ------------------------------------------------------------------------------------------------------
class StickTool:
    def __init__(self, program, port):
        self.program, self.port = program, port

    def run(self, args, timeout):
        """(exit code or None on timeout, stdout, stderr). The tool's output is used only to read markers; it is never echoed."""
        try:
            done = subprocess.run([self.program, "connect", self.port, "resume", *args], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "", "timed out"
        except OSError as error:
            return 1, "", str(error)
        return done.returncode, done.stdout.replace("\r", ""), done.stderr.replace("\r", "")

    def exec_markers(self, code, marker, end=None, timeout=EXEC_TIMEOUT_S):
        """Run a snippet; return (parsed marker payloads, finished). finished is False if the end marker never came."""
        status, out, err = self.run(["exec", code], timeout)
        items = []
        for line in out.splitlines():
            if line.startswith(marker + ":"):
                try:
                    items.append(json.loads(line[len(marker) + 1:]))
                except ValueError:
                    pass
        finished = status == 0 and (end is None or (end in out.splitlines()))
        return items, finished, (err.strip().splitlines() or ["no answer"])[-1][:200]

    def connected(self):
        return os.path.exists(self.port)


# ---- port selection: always explicit -----------------------------------------------------------------------------------------
def choose_port(requested):
    candidates = stick_config.usb_candidates(DEV_DIR)
    listing = ", ".join(candidates) if candidates else "none"
    if not requested:
        if len(candidates) == 1:
            raise Refused(f"Name the Stick's port explicitly with --port. One candidate is connected: {candidates[0]} "
                          "(this tool never picks a port by itself).")
        raise Refused(f"Name the Stick's port explicitly with --port. Connected candidates: {listing}.")
    if os.path.basename(requested).startswith(stick_config.USB_PREFIX) is False or os.path.dirname(requested) != DEV_DIR:
        raise Refused(f"{requested} does not look like a Stick port ({DEV_DIR}/{stick_config.USB_PREFIX}...). Connected candidates: {listing}.")
    if requested not in candidates:
        raise Refused(f"{requested} is not connected now. Connected candidates: {listing}.")
    return requested


# ---- identification ----------------------------------------------------------------------------------------------------------
def kit_app_files():
    """The app files this kit deploys (tools/deploy_stick.sh is the single list), with their SHA-256 in this checkout."""
    text = (ROOT / "tools" / "deploy_stick.sh").read_text()
    names = text.split("\nFILES=(")[1].split(")")[0].split()
    return {"/flash/" + n: hashlib.sha256((ROOT / "stick" / n).read_bytes()).hexdigest() for n in names}


def classify(raw):
    """Support status: 'supported' only for the validated firmware; 'unsupported' when identified otherwise; 'unknown' when not identified."""
    machine, release, version, uiflow = raw.get("machine"), raw.get("release"), raw.get("version"), raw.get("uiflow")
    build = None
    if version:
        m = re.search(r"on (\d{4}-\d{2}-\d{2})", version)
        build = m.group(1) if m else None
    uiflow = uiflow if isinstance(uiflow, str) and re.fullmatch(r"V\d+\.\d+\.\d+", uiflow) else None
    facts = {"model": machine, "micropython": release, "build_date": build, "uiflow": uiflow}
    if None in facts.values():
        missing = [k for k, v in facts.items() if v is None]
        return "unknown", facts, ["could not read: " + ", ".join(missing)]
    reasons = []
    if SUPPORTED["model"] not in machine:
        reasons.append(f"model {machine!r} is not a {SUPPORTED['model']}")
    for key in ("micropython", "build_date", "uiflow"):
        if facts[key] != SUPPORTED[key]:
            reasons.append(f"{key} {facts[key]} (supported: {SUPPORTED[key]})")
    return ("supported" if not reasons else "unsupported"), facts, reasons


def identify(tool):
    items, finished, why = tool.exec_markers(IDENTIFY_SNIPPET, "STICKID")
    if not items:
        raise Failed(f"The Stick did not answer as MicroPython ({why}). Firmware: unknown. Nothing was read or changed.")
    raw = items[-1] if isinstance(items[-1], dict) else {}
    support, facts, reasons = classify(raw)
    kit = kit_app_files()
    hashes, done, _ = tool.exec_markers(HASH_SNIPPET.replace("PATHS", json.dumps(sorted(kit))), "STICKHASH", "STICKHASH-END")
    on_device = {h[0]: h[1] for h in hashes if isinstance(h, list) and len(h) == 3}
    if not done:
        app = {"status": "unknown", "detail": "the app files could not be read completely"}
    else:
        same = sorted(p for p in kit if on_device.get(p) == kit[p])
        absent = sorted(p for p in kit if on_device.get(p) is None)
        differ = sorted(p for p in kit if on_device.get(p) not in (None, kit[p]))
        if len(same) == len(kit):
            status = "matches this kit"
        elif len(absent) == len(kit):
            status = "not installed"
        else:
            status = f"differs from this kit ({len(same)} of {len(kit)} app files identical)"
        app = {"status": status, "identical": [p[7:] for p in same], "different": [p[7:] for p in differ], "absent": [p[7:] for p in absent]}

    def known(key):
        value = raw.get(key)
        return "unknown" if value is None else value
    return {"device_id": known("id"), "model": facts["model"] or "unknown", "micropython": facts["micropython"] or "unknown",
            "micropython_build": raw.get("version") or "unknown", "uiflow": facts["uiflow"] or "unknown",
            "boot_option": known("boot_option"), "wifi_ssid_saved": known("wifi_ssid_saved"),
            "wifi_password_saved": known("wifi_password_saved"), "device_key_present": known("key_present"),
            "support": support, "support_reasons": reasons, "app": app}


def yes_no(value):
    return {True: "yes", False: "no"}.get(value, str(value))


def print_identity(port, ident):
    print(f"Stick on {port}")
    print(f"  device id          {ident['device_id']}   (derived from the chip's unique id; treat it as identifying, not secret)")
    print(f"  model              {ident['model']}")
    print(f"  MicroPython        {ident['micropython']}   ({ident['micropython_build']})")
    print(f"  UIFlow firmware    {ident['uiflow']}")
    print(f"  boot option        {ident['boot_option']}   (0 = this assistant starts by itself)")
    print(f"  Wi-Fi saved        network name: {yes_no(ident['wifi_ssid_saved'])}, password: {yes_no(ident['wifi_password_saved'])}   (presence only; values are never read out)")
    print(f"  device key         {'present' if ident['device_key_present'] is True else yes_no(ident['device_key_present'])}   (presence only)")
    print(f"  assistant app      {ident['app']['status']}")
    for label in ("different", "absent"):
        if ident["app"].get(label):
            print(f"                     {label}: {', '.join(ident['app'][label])}")
    verdict = {"supported": "SUPPORTED (the firmware this kit was validated on)",
               "unsupported": "NOT SUPPORTED: identified, but not the validated firmware",
               "unknown": "UNKNOWN: could not be identified completely"}[ident["support"]]
    print(f"  firmware support   {verdict}")
    for reason in ident["support_reasons"]:
        print(f"                     - {reason}")


# ---- backup ------------------------------------------------------------------------------------------------------------------
def backup_base(cfg):
    """<data_dir>/stick-backups, refused when it would be inside the project and not ignored by git."""
    base = Path(cfg.data_dir) / "stick-backups"
    try:
        inside = base.resolve().is_relative_to(ROOT.resolve())
    except AttributeError:                                              # Python < 3.9 has no is_relative_to; setup needs 3.9+
        inside = str(base.resolve()).startswith(str(ROOT.resolve()) + os.sep)
    if inside and (ROOT / ".git").exists():
        check = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(base / "x")], capture_output=True)
        if check.returncode != 0:
            raise Refused(f"The backup folder {base} would be inside the project and NOT ignored by git. Set data_dir to a private folder "
                          "(config/host.conf, [paths]) or make sure it is git-ignored. Nothing was contacted.")
    return base


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def new_backup_dir(device_dir):
    for _ in range(20):
        candidate = device_dir / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3))
        try:
            candidate.mkdir(mode=0o700)                                   # exclusive: an existing folder is never reused
            os.chmod(candidate, 0o700)
            return candidate
        except FileExistsError:
            continue
    raise Failed("Could not create a new backup folder.")


def write_manifest(folder, manifest):
    tmp = folder / ".manifest.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    os.replace(tmp, folder / "manifest.json")


def safe_relative(path):
    """'/flash/a/b.py' -> 'a/b.py', or None for a name that could escape the backup folder."""
    if not path.startswith("/flash/"):
        return None
    rel = path[len("/flash/"):]
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") for p in parts) or "\\" in rel or "\x00" in rel:
        return None
    return rel


def free_bytes(path):
    probe = path
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def do_backup(tool, cfg, args):
    base = backup_base(cfg)
    ident = identify(tool)
    print_identity(tool.port, ident)
    if ident["device_id"] == "unknown" or ident["support"] == "unknown":
        raise Failed("Backup refused: the Stick could not be identified completely (see above). Nothing was copied.")
    if args.expect_device and args.expect_device != ident["device_id"]:
        raise Failed(f"Backup refused: this is device {ident['device_id']}, not {args.expect_device}. Nothing was copied.")
    if ident["support"] == "unsupported" and not args.allow_unsupported:
        raise Failed("Backup refused: the firmware is not the supported version. If you still want a file copy of this Stick, run again "
                     "with --allow-unsupported (the file layout is then not guaranteed). Nothing was copied.")
    entries, finished, why = tool.exec_markers(LIST_SNIPPET, "STICKLS", "STICKLS-END")
    if not finished:
        raise Failed(f"Backup stopped: the list of files on the Stick was not complete ({why}). Nothing was copied.")
    files, excluded, skipped = [], [], []
    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 3) or entry[1] != "f":
            continue
        path, size = entry[0], entry[2]
        if key_like(path):
            excluded.append({"path": path, "reason": EXCLUDED.get(path, KEY_LIKE_REASON)})
        elif safe_relative(path) is None or not isinstance(size, int) or size < 0:
            skipped.append({"path": path, "reason": "unsafe or unreadable name or size: not copied"})
        else:
            files.append((path, size))
    need = int(sum(s for _, s in files) * SPACE_MARGIN) + SPACE_EXTRA
    have = free_bytes(base)
    if have < need:
        raise Failed(f"Backup refused: it needs about {need // 1024} KB of free space and {have // 1024} KB are free where it would be stored. "
                     "Nothing was copied.")

    device_dir = private_dir(private_dir(base) / ("stick-" + ident["device_id"]))
    folder = new_backup_dir(device_dir)
    store = private_dir(folder / "files")
    manifest = {"tool_version": TOOL_VERSION, "status": "in-progress", "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "port": tool.port, "device": {k: v for k, v in ident.items() if k != "app"}, "app": ident["app"],
                "files": [], "excluded": excluded, "skipped": skipped, "failures": [], "limits": LIMITS, "connection": IMPACT}
    write_manifest(folder, manifest)                                    # "in-progress" until the very end: a killed run stays incomplete
    print(f"\nBacking up {len(files)} files to {folder}  (private; the device key is excluded)")
    try:
        copied = []
        for index, (path, size) in enumerate(files):
            rel = safe_relative(path)
            local = store / rel
            private_dir(local.parent)
            if local.exists():
                raise Failed("refusing to overwrite " + str(local))
            status, _, err = tool.run(["fs", "--no-verbose", "cp", ":" + path, str(local)], COPY_TIMEOUT_S)
            if status != 0 or not local.is_file():
                manifest["failures"].append({"path": path, "reason": "copy failed" if status is not None else "copy timed out"})
                if not tool.connected():
                    for later, _ in files[index + 1:]:
                        manifest["failures"].append({"path": later, "reason": "not attempted: the Stick disconnected"})
                    break
                continue
            os.chmod(local, 0o600)
            data = local.read_bytes()
            copied.append(path)
            manifest["files"].append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                      "listed_size": size, "verified": False})
        if copied:
            hashes, done, why = tool.exec_markers(HASH_SNIPPET.replace("PATHS", json.dumps(copied)), "STICKHASH", "STICKHASH-END")
            on_device = {h[0]: (h[1], h[2]) for h in hashes if isinstance(h, list) and len(h) == 3}
            for record in manifest["files"]:
                device_hash, device_size = on_device.get(record["path"], (None, None))
                record["device_sha256"] = device_hash
                if device_hash is None:
                    manifest["failures"].append({"path": record["path"], "reason": "could not be verified on the Stick" + ("" if done else f" ({why})")})
                elif device_hash != record["sha256"] or device_size != record["size"]:
                    manifest["failures"].append({"path": record["path"], "reason": "copy differs from the Stick (partial or damaged transfer)"})
                else:
                    record["verified"] = True
        again, _, _ = tool.exec_markers(IDENTIFY_SNIPPET, "STICKID")
        now_id = again[-1].get("id") if again and isinstance(again[-1], dict) else None
        if now_id != ident["device_id"]:
            manifest["failures"].append({"path": None, "reason": "the Stick could not be re-identified at the end, or a different device answered"})
        complete = not manifest["failures"] and all(r["verified"] for r in manifest["files"]) and len(manifest["files"]) == len(files)
        manifest["status"] = "complete" if complete else "incomplete"
    except BaseException as error:                                       # interrupted (Ctrl-C) or an unexpected error: never "complete"
        manifest["status"] = "incomplete"
        manifest["failures"].append({"path": None, "reason": "interrupted: " + type(error).__name__})
        write_manifest(folder, manifest)
        raise
    manifest["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_manifest(folder, manifest)
    return folder, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(prog="stick_backup.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ports", help="list connected candidates (opens nothing)")
    for name in ("identify", "backup"):
        p = sub.add_parser(name)
        p.add_argument("--port", help="the Stick's serial port, e.g. /dev/cu.usbmodemXXXX (required: never chosen automatically)")
        p.add_argument("--restart", action="store_true", help="restart the Stick at the end so the assistant runs again")
        if name == "identify":
            p.add_argument("--json", action="store_true", help="print the identification as JSON")
        else:
            p.add_argument("--expect-device", help="refuse unless the Stick reports this device id")
            p.add_argument("--allow-unsupported", action="store_true", help="also back up an identified but unsupported firmware")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "ports":
            found = stick_config.usb_candidates(DEV_DIR)
            print("Connected candidates (nothing was opened): " + (", ".join(found) if found else "none"))
            return 0
        port = choose_port(args.port)
        try:
            cfg = stick_config.load(require_server=False)
        except stick_config.ConfigError as error:
            raise Refused(f"Configuration problem: {error}")
        if not os.access(cfg.mpremote, os.X_OK):
            raise Refused(f"The Stick tool was not found: {cfg.mpremote} (run the Mac setup first, or set mpremote in the configuration).")
        if args.command == "backup":
            backup_base(cfg)                                            # refuse an unsafe location before contacting the Stick
        tool = StickTool(cfg.mpremote, port)
        quiet = getattr(args, "json", False)                           # --json: stdout carries only the JSON; notes go to stderr

        def note(text):
            print(text, file=sys.stderr if quiet else sys.stdout)
        note("Note: " + IMPACT + "\n")
        try:
            if args.command == "identify":
                ident = identify(tool)
                if args.json:
                    print(json.dumps(ident, indent=2, sort_keys=True))
                else:
                    print_identity(port, ident)
                code = 0
            else:
                folder, manifest = do_backup(tool, cfg, args)
                ok = manifest["status"] == "complete"
                print(f"\nBackup {'COMPLETE' if ok else 'INCOMPLETE'}: {len(manifest['files'])} files copied, "
                      f"{sum(1 for r in manifest['files'] if r['verified'])} verified; excluded: {', '.join(e['path'] for e in manifest['excluded']) or 'none'}")
                for failure in manifest["failures"]:
                    print(f"  problem: {failure['path'] or '(backup)'}: {failure['reason']}")
                print(f"Manifest: {folder / 'manifest.json'}")
                for line in LIMITS:
                    print("  * " + line)
                code = 0 if ok else 1
        finally:
            if args.restart and tool.connected():
                tool.run(["exec", "--no-follow", "import machine; machine.reset()"], EXEC_TIMEOUT_S)
                note("The Stick was asked to restart.")
            elif tool.connected():
                note("The assistant app is stopped until the Stick restarts. Run again with --restart, or restart it with:\n  "
                     f"{tool.program} connect {port} resume exec --no-follow \"import machine; machine.reset()\"\n"
                     "(Unplugging may not restart it: the Stick has a battery.)")
        return code
    except Refused as error:
        print(str(error), file=sys.stderr)
        return 2
    except Failed as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
