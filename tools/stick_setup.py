#!/usr/bin/env python3
"""Initial setup of a StickS3 after its files are installed: Wi-Fi checks, device-key provisioning, auto-start and pairing (docs/stick-setup.md).

    python3 tools/stick_setup.py plan                                    OFFLINE: the setup order and this Mac's side (opens no port)
    python3 tools/stick_setup.py status    --port /dev/cu.usbmodemXXXX   read-only: every setup step's state, kept apart
    python3 tools/stick_setup.py wifi      --port /dev/cu.usbmodemXXXX   bounded check ON THE STICK with its saved Wi-Fi (writes nothing)
          [--reuse-only]       only use a connection the Stick already has: never switch Wi-Fi on or join a network
    python3 tools/stick_setup.py key       --port … --expect-device ID [--backup FOLDER]   give the Stick this installation's device key
    python3 tools/stick_setup.py autostart --port … --expect-device ID (--on | --off | --check) [--backup FOLDER]
          --check (key, autostart)   CHECK ONLY: report and compare, never write anything (not even when a backup is given)
    python3 tools/stick_setup.py verify    --port … --expect-device ID [--timeout S]   restart the Stick; wait for a fresh authenticated
                                                                                      report from a NEW boot
          --expect-device ID   refuse unless the Stick reports this device id (required for key, autostart and verify)
          --backup FOLDER      a complete backup of THIS Stick (tools/stick_backup.py); required whenever something would be written

WI-FI is set ON THE STICK with UIFlow's own Setup app (its hotspot and web page): this Mac never asks for, stores or reads Wi-Fi
credentials, and nothing here writes them. `wifi` only uses what is already saved, on the Stick, and reports what happened.

THE DEVICE KEY lives in this installation's private secrets folder (secrets_dir in config/host.conf) and in /flash/device_key.txt.
It is never printed, logged, passed on a command line, copied into a backup or committed. Keys are compared with a fresh random
salt: the Stick returns SHA-256(salt + key) and this Mac compares it with its own; neither the key nor a reusable hash leaves either
side. An existing key on the Stick is never replaced or deleted: a different key stops the run (rotation is not implemented).

AUTO-START is UIFlow's boot_option (NVS): 0 runs /flash/main.py at power-on. `autostart` changes only that one value, keeps the
previous value in a private record, and reads it back. File backups do NOT contain it. It counts as verified only after `verify`
has seen the app report from a new boot.

PAIRING counts as confirmed only when the Mac server shows a fresh authenticated report from a new boot that began after this tool
restarted the identified Stick. A key file being present, or even matching, is not pairing.
"""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stick_backup as sb  # noqa: E402
import stick_install as si  # noqa: E402
import stick_restore as sr  # noqa: E402

ROOT = sb.ROOT
Refused, Failed = sb.Refused, sb.Failed
KEY_FILE = sb.KEY_FILE
KEY_PART = KEY_FILE + ".part"          # the partial copy before it is moved into place (named like the key: never backed up)
MIN_KEY_CHARS = 32                     # the server ignores shorter keys (device_link.MIN_KEY_CHARS)
STATUS_TIMEOUT_S = 4
WIFI_TIMEOUT_S = 20                    # the same bound stick/main.py uses at start-up
NEVER = [
    "Wi-Fi credentials: never asked for, written, read out or stored on this Mac; Wi-Fi is set on the Stick with UIFlow's Setup app.",
    "The device key: never printed, logged, put on a command line, backed up or committed; an existing key on the Stick is never "
    "replaced or deleted.",
    "NVS: only boot_option is ever written, and only by `autostart`.",
    "Firmware, files outside the key file, the server and its settings: never changed. The server is never restarted.",
]
LIMITS = [
    "Pairing is attributed to this Stick by timing: the report comes from a new boot that began after this tool restarted the "
    "identified Stick. Reports carry no device identity.",
    "Tested against a simulated Stick and an isolated server only.",
]
WIFI_STATUS = {1000: "idle", 1001: "connecting", 1010: "connected (got an address)", 200: "beacon timeout", 201: "network not found",
               202: "wrong password", 203: "association failed", 204: "handshake timeout"}

# ---- device-side snippets (MicroPython). Never print a key, a Wi-Fi name or a password.
KEY_SNIPPET = r'''
import os, json, hashlib, binascii
def look(p):
    try:
        os.stat(p)
    except OSError:
        return ["absent", None, None]
    try:
        with open(p, "rb") as f:
            b = f.read().strip()
        return ["present", len(b) >= MIN, binascii.hexlify(hashlib.sha256(SALT.encode() + b).digest()).decode()]
    except Exception:
        return ["unreadable", None, None]
print("STICKKEY:" + json.dumps({"final": look(FINAL), "part": look(PART)}))
'''

KEY_ACTIVATE_SNIPPET = r'''
import os, json
try:
    os.stat(FINAL)
    print("STICKKEYACT:" + json.dumps("exists"))
except OSError:
    os.rename(PART, FINAL)
    print("STICKKEYACT:" + json.dumps("activated"))
'''

BOOT_SET_SNIPPET = r'''
import esp32, json
n = esp32.NVS("uiflow")
n.set_u8("boot_option", VALUE)
n.commit()
print("STICKBOOT:" + json.dumps(esp32.NVS("uiflow").get_u8("boot_option")))
'''

WIFI_SNIPPET = r'''
import network, time, json, esp32
r = {}
w = network.WLAN(network.STA_IF)
if not REUSE_ONLY:
    w.active(True)
r["already_connected"] = bool(w.isconnected())
if not r["already_connected"] and not REUSE_ONLY:
    n = esp32.NVS("uiflow")
    w.connect(n.get_str("ssid0"), n.get_str("pswd0"))
    n = None
    stop = [getattr(network, k, None) for k in ("STAT_WRONG_PASSWORD", "STAT_NO_AP_FOUND")]
    start = time.ticks_ms()
    while not w.isconnected() and time.ticks_diff(time.ticks_ms(), start) < TIMEOUT_MS:
        if w.status() in stop:
            break
        time.sleep_ms(250)
r["connected"] = bool(w.isconnected())
r["status"] = w.status()
r["server"] = None
if r["connected"]:
    try:
        with open("/flash/device_config.json") as f:
            c = json.loads(f.read())
        host, port = c["server_host"], int(c["server_port"])
        r["server"] = [host, port]
        import socket
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(socket.getaddrinfo(host, port)[0][-1])
            r["server_port_open"] = True
        except Exception:
            r["server_port_open"] = False
        s.close()
    except Exception:
        r["server_port_open"] = None
print("STICKWIFI:" + json.dumps(r))
'''


def fill(snippet, **values):
    return sr.fill(snippet, **values)


# ---- this Mac's side ----------------------------------------------------------------------------------------------------------
def key_path(cfg):
    return Path(cfg.secrets_dir) / "device_key"


def check_secrets_folder(cfg):
    """The private key storage of this installation: owner-only, and never inside the project unless git ignores it."""
    folder = Path(cfg.secrets_dir)
    inside = str(folder.resolve()).startswith(str(ROOT.resolve()) + os.sep)
    if inside and (ROOT / ".git").exists():
        if subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(folder / "device_key")], capture_output=True).returncode != 0:
            raise Refused(f"Refused: the secrets folder {folder} is inside the project and NOT ignored by git. Nothing was contacted.")
    if folder.exists() and folder.stat().st_mode & 0o077:
        raise Refused(f"Refused: the secrets folder {folder} is readable by other users (it must be 0700). Nothing was contacted.")
    path = key_path(cfg)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise Refused(f"Refused: {path} is not a plain file. Nothing was contacted.")
        if path.stat().st_mode & 0o077:
            raise Refused(f"Refused: the key file {path} is readable by other users (run chmod 600). Nothing was contacted.")
    return path


def mac_key(cfg):
    """This installation's key (stripped bytes), or None when it has none. The value is never printed."""
    path = check_secrets_folder(cfg)
    if not path.exists():
        return None
    data = path.read_bytes().strip()
    if len(data) < MIN_KEY_CHARS:
        raise Refused(f"Refused: the key file {path} is too short to be used (the server would ignore it). Nothing was contacted.")
    return data


def create_mac_key(cfg):
    """Create this installation's key once (atomic, 0600). Never replaces an existing one, so a retry keeps the same key."""
    path = key_path(cfg)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".device_key.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_hex(32) + "\n")
        os.chmod(tmp, 0o600)
        os.link(tmp, path)                      # fails if a key appeared meanwhile: never overwrite one
    finally:
        os.unlink(tmp)
    return path.read_bytes().strip()


def salted(salt, key):
    return hashlib.sha256(salt.encode() + key).hexdigest()


def server_status(cfg):
    """(status dict or None, reason). The panel's read-only status of this installation's server."""
    url = f"http://127.0.0.1:{cfg.panel_port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=STATUS_TIMEOUT_S) as answer:
            data = json.loads(answer.read().decode())
        if not isinstance(data, dict) or not isinstance(data.get("device"), dict):
            return None, "the server's status was not understood"
        return data, None
    except (OSError, ValueError, urllib.error.URLError) as error:
        return None, f"the server's status could not be read at {url} ({type(error).__name__})"


def server_lines(status, why):
    if status is None:
        return [f"Server: UNKNOWN: {why}. Is the server running (Start Voice Assistant)?"]
    dev = status["device"]
    report = dev.get("report") or {}
    lines = ["Server: key loaded: " + ("yes" if status.get("key_provisioned") else "NO (it reads the key only when it starts)")]
    lines.append(f"Server: last authenticated report: {dev.get('contact')}" + (f", boot {report.get('boot')}, uptime {report.get('uptime')} s"
                                                                             if report else "") +
                 " (reports carry no device identity: this alone does not show which Stick sent it)")
    return lines


# ---- the Stick ----------------------------------------------------------------------------------------------------------------
def connect(cfg, args):
    port = sb.choose_port(args.port)
    if not os.access(cfg.mpremote, os.X_OK):
        raise Refused(f"The Stick tool was not found: {cfg.mpremote}.")
    return sb.StickTool(cfg.mpremote, port), port


def key_state(tool, key):
    """The Stick's key file and any partial copy, each as {state, usable, same}. same: True/False, or None when unknown."""
    salt = secrets.token_hex(16)
    items, done, why = tool.exec_markers(fill(KEY_SNIPPET, salt=salt, min=MIN_KEY_CHARS, final=KEY_FILE, part=KEY_PART), "STICKKEY")
    if not done or not items or not isinstance(items[-1], dict):
        raise Failed(f"Could not check the device key on the Stick ({why}). Nothing was written.")
    out = {}
    for name in ("final", "part"):
        state, usable, digest = (items[-1].get(name) or ["unreadable", None, None])
        out[name] = {"state": state, "usable": usable, "same": None if key is None or digest is None else digest == salted(salt, key)}
    return out


def require_backup(args, ident):
    if not args.backup:
        raise Failed("Refused: this would change the Stick, so a complete backup of THIS Stick is required first: take one with "
                     "tools/stick_backup.py backup and pass it with --backup. Nothing was written.")
    manifest, _, _ = sr.validate_backup(Path(args.backup).expanduser())
    if manifest["device"]["device_id"] != ident["device_id"]:
        raise Failed(f"Refused: the backup belongs to device {manifest['device']['device_id']}, not this Stick. Nothing was written.")
    print(f"Backup checked: {args.backup} is a complete backup of this Stick.")
    return {"folder": str(Path(args.backup).expanduser().resolve()), "made_utc": manifest.get("created_utc")}


def record(cfg, ident, kind):
    rec = sr.Record(sr.record_base(cfg, "stick-setup"), ident["device_id"], kind=kind, never=NEVER, limits=LIMITS)
    rec.data["device"] = {k: v for k, v in ident.items() if k != "app"}
    rec.save()
    return rec


def restart_hint(tool, port):
    if tool.connected():
        print("The assistant app (if any) is stopped until the Stick restarts (or run `verify`, which restarts it):\n  "
              + sr.RESTART_CMD.format(program=tool.program, port=port))


def install_check(tool, cfg):
    """(state text, every file matches?) for the install set (tools/stick_install.py)."""
    _, entries = si.build_set(cfg)
    result, _, _, _ = sr.compare(tool, entries)
    return si.install_state(result, entries), not (result["change"] or result["missing"] or result["conflict"])


def yes_no(value):
    return {True: "yes", False: "no"}.get(value, "unknown") if isinstance(value, bool) or value is None else str(value)


# ---- commands -----------------------------------------------------------------------------------------------------------------
ORDER = [
    ("Mac", "Mac setup: config/host.conf, the server running (docs/mac-setup.md)."),
    ("Wi-Fi", "Wi-Fi, ON THE STICK, with UIFlow's Setup app, while the Stick still shows UIFlow's startup menu (before auto-start). "
              "No Mac tool may hold the USB port meanwhile: the firmware prints what you save to the USB console."),
    ("Backup", "A complete backup of the Stick: tools/stick_backup.py backup --port … (before any change)."),
    ("Files", "The assistant's files: tools/stick_install.py preflight, then apply (docs/stick-install.md)."),
    ("Key", "The device key: stick_setup.py key. If this Mac's key is new, the server must be restarted to load it (by you)."),
    ("Auto-start", "stick_setup.py autostart --on (boot_option 0)."),
    ("Verify", "stick_setup.py verify: restarts the Stick and waits for a fresh authenticated report from a new boot."),
    ("Voice", "A physical check: a silent recording, a spoken question and answer, automatic sleep."),
]


def cmd_plan(cfg):
    print("OFFLINE SETUP PLAN: no Stick was contacted.\n\nSETUP ORDER:")
    for index, (name, text) in enumerate(ORDER, 1):
        print(f"  {index}. {name}: {text}")
    print("\nNEVER hold button A while the Stick starts once main.py is installed: on UIFlow2 2.4.8 that REMOVES /flash/main.py "
          "(docs/stick-setup.md). To reach UIFlow's Setup app on an installed Stick, use `autostart --off` instead.")
    print("\nTHIS MAC:")
    try:
        key = mac_key(cfg)
        print(f"  Device key of this installation: {'present' if key else 'none yet'} ({key_path(cfg)}; never shown)")
    except Refused as error:
        print("  Device key: " + str(error))
    status, why = server_status(cfg)
    for line in server_lines(status, why):
        print("  " + line)
    print("\nNext: status --port <port> (read-only).")
    return 0


def cmd_status(cfg, args):
    if getattr(args, "json", False):
        import contextlib
        out = {"read_only": True, "ok": False}
        with contextlib.redirect_stdout(sys.stderr):
            try:
                out.update(status_facts(cfg, args))
                out["ok"] = True
            except (Refused, Failed) as error:
                out["error"] = str(error)
                print(str(error))
        print(json.dumps(out, sort_keys=True))
        return 0 if out["ok"] else 1
    key = mac_key(cfg)
    tool, port = connect(cfg, args)
    print("Note: " + sb.IMPACT + "\n")
    try:
        ident = si.check_device(tool, args.expect_device)
        state, complete = install_check(tool, cfg)
        keys = key_state(tool, key)
        status, why = server_status(cfg)
        boot = ident.get("boot_option")
        print("\nSETUP STATE (read-only; each line is a separate fact):")
        print(f"  Files:              {state}")
        print(f"  Wi-Fi credentials:  name saved {yes_no(ident['wifi_ssid_saved'])}, password saved {yes_no(ident['wifi_password_saved'])}"
              "   (saved is not connected: run `wifi`)")
        same = keys["final"]["same"]
        print(f"  Device key:         on the Stick {keys['final']['state']}; this Mac {'present' if key else 'none'}; same key "
              + ("yes" if same else "NO" if same is False else "unknown"))
        if keys["part"]["state"] != "absent":
            print(f"  Partial key copy:   {KEY_PART} is on the Stick (an interrupted provisioning; `key` finishes or replaces it)")
        print(f"  Auto-start:         boot_option {boot} ({'starts the assistant by itself' if boot == 0 else 'does NOT start the assistant by itself'});"
              " counted as verified only after `verify`")
        for line in server_lines(status, why):
            print("  " + line)
        print("  Pairing:            NOT confirmed by this command (only `verify` confirms it)")
        print("\nNEXT STEP: " + next_step(ident, complete, keys, key, status))
        print("\nNothing was written.")
        return 0
    finally:
        restart_hint(tool, port)


def status_facts(cfg, args):
    """The facts `status` prints, as data (read-only). Pairing is never inferred here."""
    key = mac_key(cfg)
    tool, port = connect(cfg, args)
    print("Note: " + sb.IMPACT + "\n")
    try:
        ident = si.check_device(tool, args.expect_device)
        state, complete = install_check(tool, cfg)
        keys = key_state(tool, key)
        status, why = server_status(cfg)
        return {"device_id": ident["device_id"], "files_complete": complete, "files_state": state,
                "wifi_saved": ident["wifi_ssid_saved"] is True and ident["wifi_password_saved"] is True,
                "boot_option": ident.get("boot_option"), "key_on_stick": keys["final"]["state"], "key_on_mac": key is not None,
                "same_key": keys["final"]["same"], "partial_key_copy": keys["part"]["state"] != "absent",
                "server_status_readable": status is not None, "server_key_loaded": bool(status and status.get("key_provisioned")),
                "pairing_confirmed": False, "next_step": next_step(ident, complete, keys, key, status)}
    finally:
        restart_hint(tool, port)


def next_step(ident, complete, keys, key, status):
    if not (ident["wifi_ssid_saved"] is True and ident["wifi_password_saved"] is True):
        return ("save Wi-Fi with UIFlow's Setup app on the Stick" + ("" if ident.get("boot_option") != 0 else
                " (it starts the app now: run `autostart --off` first to get UIFlow's startup menu)"))
    if not complete:
        return "install the files: tools/stick_install.py preflight, then apply"
    if keys["final"]["state"] != "present" or keys["final"]["same"] is not True:
        return "provision the device key: stick_setup.py key" if keys["final"]["state"] == "absent" else "resolve the device key (see `key`)"
    if status is None or not status.get("key_provisioned"):
        return "start (or restart) the server so it loads this installation's key"
    if ident.get("boot_option") != 0:
        return "turn auto-start on: stick_setup.py autostart --on"
    return "stick_setup.py verify (restart and wait for a fresh authenticated report)"


def cmd_wifi(cfg, args):
    tool, port = connect(cfg, args)
    print("Note: " + sb.IMPACT + "\nThis check uses the Wi-Fi saved ON THE STICK; its name and password are never read out.\n")
    try:
        ident = si.check_device(tool, args.expect_device)
        saved = ident["wifi_ssid_saved"] is True and ident["wifi_password_saved"] is True
        print("\nWI-FI CHECK (bounded; writes nothing):")
        print(f"  1. Credentials saved:      name {yes_no(ident['wifi_ssid_saved'])}, password {yes_no(ident['wifi_password_saved'])}")
        if not saved:
            print("  2. Network connected:      not tried (nothing complete is saved)")
            print("  3. Server port reachable:  not tried")
            print("\nSave Wi-Fi with UIFlow's Setup app on the Stick (docs/stick-setup.md).")
            return 1
        items, done, why = tool.exec_markers(fill(WIFI_SNIPPET, timeout_ms=WIFI_TIMEOUT_S * 1000, reuse_only=1 if args.reuse_only else 0), "STICKWIFI",
                                             timeout=sb.EXEC_TIMEOUT_S + WIFI_TIMEOUT_S + 10)
        if not done or not items or not isinstance(items[-1], dict):
            print(f"  2. Network connected:      UNKNOWN (the check did not finish: {why})")
            print("  3. Server port reachable:  UNKNOWN")
            return 1
        r = items[-1]
        code = r.get("status")
        print(f"  2. Network connected:      {'yes' if r.get('connected') else 'NO'}"
              + (" (it already was; reused)" if r.get("already_connected") else
                 " (not joined: --reuse-only never switches Wi-Fi on or joins)" if args.reuse_only else f" (within {WIFI_TIMEOUT_S} s)")
              + f"; Wi-Fi status: {WIFI_STATUS.get(code, f'unknown ({code})')}")
        if not r.get("connected"):
            print("  3. Server port reachable:  not tried")
            print("\nThe saved Wi-Fi did not connect. Check the network is in range and on a band the Stick supports, or save it again "
                  "with UIFlow's Setup app (docs/stick-setup.md).")
            return 1
        server = r.get("server")
        if server is None:
            print("  3. Server port reachable:  UNKNOWN (/flash/device_config.json could not be read on the Stick: install the files)")
            return 1
        expected = [cfg.advertise_host, cfg.device_port]
        print(f"  3. Server port reachable:  {yes_no(r.get('server_port_open'))} at {server[0]}:{server[1]}"
              + ("" if server == expected else f"  (this Mac's configuration says {expected[0]}:{expected[1]}: reinstall the files)"))
        print("  4. Authenticated contact:  not checked here (it needs the device key: run `verify`)")
        return 0 if r.get("server_port_open") else 1
    finally:
        restart_hint(tool, port)


def cmd_key(cfg, args):
    key = mac_key(cfg)
    tool, port = connect(cfg, args)
    print("Note: " + (sb.IMPACT if args.check else sb.IMPACT.replace("Nothing is written to /flash or NVS.",
                                                                      "`key` may write /flash/device_key.txt (never replacing one).")) + "\n")
    rec = None
    try:
        ident = si.check_device(tool, args.expect_device)
        keys = key_state(tool, key)
        final, part = keys["final"], keys["part"]
        print("\nDEVICE KEY (values never shown):")
        print(f"  this Mac ({key_path(cfg)}): {'present' if key else 'none yet'}")
        print(f"  the Stick ({KEY_FILE}): {final['state']}" + ("" if final["same"] is None else
                                                            f"; same as this Mac's: {'yes' if final['same'] else 'NO'}"))
        if args.check:
            if final["state"] == "present" and final["same"] is True:
                print("\nCHECK: the Stick holds this installation's key (salted comparison). That is NOT pairing: `verify` confirms it.")
                return 0
            why = ("this installation has no key" if key is None else "the Stick has no key file" if final["state"] == "absent"
                   else "the Stick's key file could not be read" if final["state"] != "present" else "the keys are DIFFERENT")
            print(f"\nCHECK FAILED: {why}. Check-only: nothing was created, replaced or repaired.")
            return 1
        if final["state"] == "present":
            if final["same"] is True:
                print("\nNothing to do: the Stick already has this installation's key. That is NOT pairing yet: run `verify`.")
                if part["state"] != "absent":
                    print(f"  (a partial copy {KEY_PART} is also on the Stick; it is not used)")
                return 0
            if key is None:
                raise Failed("Stopped: the Stick already has a device key, and this installation has none. It was probably paired "
                             "with another installation, or this installation's secrets folder was lost or moved. Nothing was "
                             "written and no key was created. If this Mac should own it: point secrets_dir in config/host.conf at "
                             "that installation's secrets folder. Replacing the Stick's key is not implemented.")
            raise Failed("Stopped: the Stick's key is DIFFERENT from this installation's. Nothing was written. Check that "
                         "config/host.conf points at the right secrets folder (the one the server uses). Replacing the Stick's key "
                         "(rotation) is not implemented: whatever else holds the Stick's key would stop working.")
        if final["state"] != "absent" or final["usable"] is False:
            raise Failed(f"Stopped: {KEY_FILE} on the Stick could not be read. Nothing was written.")
        backup = require_backup(args, ident)
        rec = record(cfg, ident, "device-key")
        rec.data["backup"] = backup
        if key is None:
            rec.data["mac_key"] = "created"
            rec.save()
            key = create_mac_key(cfg)
            print(f"Created this installation's device key: {key_path(cfg)} (0600; not shown). Retries reuse it.")
        else:
            rec.data["mac_key"] = "existing"
            rec.save()
        print(f"Copying it to {KEY_PART} on the Stick, then checking it there before moving it into place.")
        status, _, err = tool.run(["fs", "--no-verbose", "cp", str(key_path(cfg)), ":" + KEY_PART], sb.COPY_TIMEOUT_S)
        if status != 0:
            rec.data["failures"].append({"path": KEY_PART, "reason": "copy to the Stick failed"})
            raise Failed(f"The copy to the Stick failed. {KEY_FILE} was not created; run `key` again (it reuses the same key).")
        checked = key_state(tool, key)
        if checked["part"]["same"] is not True:
            rec.data["failures"].append({"path": KEY_PART, "reason": "the copy on the Stick does not match"})
            raise Failed(f"The copy on the Stick did not verify. {KEY_FILE} was not created; run `key` again (the partial copy is replaced).")
        items, done, why = tool.exec_markers(fill(KEY_ACTIVATE_SNIPPET, final=KEY_FILE, part=KEY_PART), "STICKKEYACT")
        if not done or items[-1:] != ["activated"]:
            rec.data["failures"].append({"path": KEY_FILE, "reason": "not moved into place: " + (str(items[-1:]) if items else why)})
            raise Failed(f"The key could not be moved into place ({why if not items else items[-1]}). Run `status` to see the state.")
        after = key_state(tool, key)
        if after["final"]["same"] is not True:
            rec.data["failures"].append({"path": KEY_FILE, "reason": "does not match after it was moved into place"})
            raise Failed(f"{KEY_FILE} does not match this installation's key after it was moved into place. Run `status`.")
        rec.data["device_key"] = "created"
        rec.finish("complete", summary="device key installed and checked on the Stick; pairing not yet confirmed")
        print(f"\nDEVICE KEY INSTALLED: {KEY_FILE} matches this installation's key. Pairing is NOT confirmed yet.")
        status_now, why = server_status(cfg)
        if rec.data["mac_key"] == "created" or status_now is None or not status_now.get("key_provisioned"):
            print("Next: (re)start the server so it loads this installation's key (this tool never restarts it), then `verify`.")
        else:
            print("Next: `verify`. (If the server started before this key existed, restart it first: it reads the key only at start-up.)")
        return 0
    except Failed:
        if rec:
            rec.finish("failed")
        raise
    finally:
        restart_hint(tool, port)


def cmd_autostart(cfg, args):
    target = 0 if (args.on or args.check) else 1
    tool, port = connect(cfg, args)
    print("Note: " + (sb.IMPACT if args.check else sb.IMPACT.replace("Nothing is written to /flash or NVS.",
                                                                      "`autostart` may write ONE NVS value, boot_option.")) + "\n")
    rec = None
    try:
        ident = si.check_device(tool, args.expect_device)
        current = ident.get("boot_option")
        meaning = {0: "runs /flash/main.py: the assistant starts by itself", 1: "UIFlow's startup menu (and its Setup app)",
                   2: "UIFlow network setup only", "not set": "not set (UIFlow then shows its startup menu)"}
        print(f"\nAUTO-START: boot_option is now {current}: {meaning.get(current, 'unknown')}.")
        if current == "unknown" or current is None:
            raise Failed("Stopped: the current boot_option could not be read. Nothing was written.")
        if args.check:
            print("CHECK: " + ("auto-start is ON (boot_option 0). Set, not yet observed: `verify` restarts the Stick and watches for it."
                               if current == 0 else "auto-start is OFF. Check-only: nothing was written."))
            return 0 if current == 0 else 1
        if current == target:
            print(f"Nothing to do: boot_option is already {target}." + (" Run `verify` to see it start." if target == 0 else ""))
            return 0
        if target == 0:
            state, complete = install_check(tool, cfg)
            key = mac_key(cfg)
            keys = key_state(tool, key)
            problems = []
            if not complete:
                problems.append(f"the installed files are not complete ({state}): run tools/stick_install.py")
            if not (ident["wifi_ssid_saved"] is True and ident["wifi_password_saved"] is True):
                problems.append("Wi-Fi is not saved: save it with UIFlow's Setup app first (it is reached through the startup menu)")
            if keys["final"]["same"] is not True:
                problems.append("the Stick does not hold this installation's device key: run `key` first")
            if problems:
                raise Failed("Refused: auto-start comes last. " + "; ".join(problems) + ". Nothing was written.")
            print("Checked: every installed file matches, Wi-Fi is saved, and the Stick holds this installation's key.")
        else:
            print("Turning auto-start off shows UIFlow's startup menu at the next start (the safe way to reach its Setup app). "
                  "main.py and every other file stay as they are.")
        backup = require_backup(args, ident)
        rec = record(cfg, ident, "auto-start")
        rec.data.update(backup=backup, boot_option_before=current, boot_option_target=target)
        rec.save()
        print(f"Recorded the previous value ({current}) in {rec.folder / 'record.json'}. File backups do not contain this setting.")
        items, done, why = tool.exec_markers(fill(BOOT_SET_SNIPPET, value=target), "STICKBOOT")
        check = sb.identify(tool)
        after = check.get("boot_option")
        rec.data["boot_option_after"] = after
        if after != target:
            rec.data["failures"].append({"path": None, "reason": f"boot_option reads back {after}" + ("" if done else f" ({why})")})
            raise Failed(f"The setting did not take: boot_option reads back {after} (wanted {target}). The previous value was "
                         f"{current} (recorded). Run `autostart` again or check the Stick.")
        rec.finish("complete", summary=f"boot_option {current} -> {target}, read back")
        print(f"\nboot_option is now {target} (read back)." + (
            " Auto-start is SET but NOT verified: run `verify` to restart the Stick and see the assistant report from a new boot."
            if target == 0 else " The next start shows UIFlow's startup menu. Turn it back on with `autostart --on` afterwards."))
        return 0
    except Failed:
        if rec:
            rec.finish("failed")
        raise
    finally:
        restart_hint(tool, port)


def cmd_verify(cfg, args):
    key = mac_key(cfg)
    if key is None:
        raise Refused("Refused: this installation has no device key yet (run `key`). Nothing was contacted.")
    before, why = server_status(cfg)
    if before is None:
        raise Refused(f"Refused: pairing cannot be confirmed without the server: {why}. Nothing was contacted or restarted.")
    if not before.get("key_provisioned"):
        raise Refused("Refused: the server has no device key loaded (it reads it only at start-up): restart the server, then `verify`. "
                      "Nothing was contacted or restarted.")
    tool, port = connect(cfg, args)
    print("Note: `verify` stops the app, checks the Stick, then RESTARTS it and waits for its first report.\n")
    try:
        ident = si.check_device(tool, args.expect_device)
        keys = key_state(tool, key)
    except Failed:
        restart_hint(tool, port)
        raise
    if keys["final"]["same"] is not True:
        restart_hint(tool, port)
        raise Failed("Stopped: the Stick does not hold this installation's device key, so it could not authenticate (run `status`). "
                     "It was not restarted.")
    old_boot = ((before["device"].get("report") or {}).get("boot"))
    rec = record(cfg, ident, "verification")
    rec.data.update(boot_option=ident.get("boot_option"), boot_before=old_boot)
    rec.save()
    tool.run(["exec", "--no-follow", "import machine; machine.reset()"], sb.EXEC_TIMEOUT_S)
    restarted = time.time()
    print(f"Restarted the Stick ({ident['device_id']}). Waiting up to {args.timeout} s for a fresh authenticated report from a new boot…")
    seen = None
    while time.time() - restarted < args.timeout:
        time.sleep(min(2.0, max(0.2, args.timeout / 30)))
        status, _ = server_status(cfg)
        if status is None:
            continue
        dev, report = status["device"], status["device"].get("report") or {}
        uptime = report.get("uptime")
        if (dev.get("contact") == "fresh" and report.get("boot") and report.get("boot") != old_boot and isinstance(uptime, (int, float))
                and uptime <= time.time() - restarted + 5):
            seen = (report.get("boot"), round(time.time() - restarted), uptime)
            break
    auto = ident.get("boot_option") == 0
    if seen:
        rec.data.update(paired=True, autostart_observed=auto, boot_after=seen[0], seconds=seen[1])
        rec.finish("complete", summary="fresh authenticated report from a new boot after the restart")
        print("\nVERIFY:")
        print(f"  Authenticated server contact: CONFIRMED: a fresh authenticated report from a new boot, {seen[1]} s after this tool "
              "restarted the identified Stick (its uptime fits that restart).")
        print("  Network connected:            yes (implied by that report)")
        print("  Pairing (device key):         CONFIRMED for this Stick, by that timing (reports carry no device identity)")
        print("  Auto-start:                   " + ("OBSERVED: the app started by itself after a restart (boot_option 0)" if auto
                                                   else "not on (boot_option is not 0)"))
        print("Still to do yourself: a short voice check (a silent recording, a spoken question and answer, automatic sleep).")
        return 0
    rec.data.update(paired=False, autostart_observed=False)
    rec.finish("failed", summary=f"no fresh authenticated report from a new boot within {args.timeout} s")
    print(f"\nVERIFY: NOT confirmed: no fresh authenticated report from a new boot within {args.timeout} s.")
    print("  What is known: the Stick holds this installation's key, and the server has a key loaded.")
    print("  Unknown: whether the app started, Wi-Fi connected and the server was reached." +
          ("" if auto else f" boot_option is {ident.get('boot_option')} (not 0), so the app does not start by itself: `autostart --on`."))
    print("  Next: `wifi` (a bounded check of Wi-Fi and the server port from the Stick), and the server's console for "
          "'authentication failed' (a key the server did not load: restart the server).")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="stick_setup.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="offline: the setup order and this Mac's side (opens no port)")
    for name in ("status", "wifi", "key", "autostart", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--port", help="the Stick's serial port (required: never chosen automatically)")
        p.add_argument("--expect-device", required=name in ("key", "autostart", "verify"), help="refuse unless the Stick reports this device id")
        if name in ("key", "autostart"):
            p.add_argument("--backup", help="a complete backup of THIS Stick; required whenever something would be written")
        if name == "key":
            p.add_argument("--check", action="store_true", help="check only: compare the keys, never create, copy or replace anything")
        if name == "wifi":
            p.add_argument("--reuse-only", action="store_true", help="only use an existing connection; never switch Wi-Fi on or join")
        if name == "autostart":
            group = p.add_mutually_exclusive_group(required=True)
            group.add_argument("--on", action="store_true", help="boot_option 0: the assistant starts by itself")
            group.add_argument("--off", action="store_true", help="boot_option 1: UIFlow's startup menu (to reach its Setup app)")
            group.add_argument("--check", action="store_true", help="check only: show boot_option, never write it")
        if name == "status":
            p.add_argument("--json", action="store_true", help="print the facts as JSON on stdout (the readable report goes to stderr)")
        if name == "verify":
            p.add_argument("--timeout", type=int, default=90, help="seconds to wait for the first report (default 90)")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        try:
            cfg = sb.stick_config.load(require_server=False)
        except sb.stick_config.ConfigError as error:
            raise Refused(f"Configuration problem: {error}")
        if args.command == "plan":
            return cmd_plan(cfg)
        return {"status": cmd_status, "wifi": cmd_wifi, "key": cmd_key, "autostart": cmd_autostart, "verify": cmd_verify}[args.command](cfg, args)
    except Refused as error:
        print(str(error), file=sys.stderr)
        return 2
    except Failed as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
