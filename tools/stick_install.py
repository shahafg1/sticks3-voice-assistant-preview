#!/usr/bin/env python3
"""Install the assistant's complete FILE set on a StickS3 that already runs the supported UIFlow firmware (docs/stick-install.md).

    python3 tools/stick_install.py plan                                     OFFLINE: check every source file and list the install set
    python3 tools/stick_install.py preflight --port /dev/cu.usbmodemXXXX    read-only: identify the Stick, compare, check space
    python3 tools/stick_install.py apply     --port /dev/cu.usbmodemXXXX    write the files that are missing or differ
          [--backup FOLDER]          a complete backup of THIS Stick (tools/stick_backup.py); required when existing files would be replaced
          [--expect-device ID]       refuse unless the Stick reports this device id
          [--expect-changes PATH]    refuse before any write unless exactly these files would be written (repeatable)
          [--restart]                restart the Stick at the end, only when that is safe
    python3 tools/stick_install.py remove-created RECORD --port /dev/cu.usbmodemXXXX --expect-remove PATH [--expect-remove PATH ...]
          RECOVERY ONLY: remove the files that installation RECORD (its operation folder) recorded as newly created, and nothing else.
          Every created file must be named with --expect-remove, and each is removed only if it still has the exact SHA-256 the
          installation wrote; if any differs, nothing is removed. Restore replaced files first with tools/stick_restore.py --only.
    python3 tools/stick_install.py pin       (maintainers) re-pin the source hashes in stick/install_manifest.json after a reviewed change

The install set is stick/install_manifest.json: every app module, main.py, the generated device configuration (server address and
port only) and the sound files. Sources come from this checkout only, never from a backup or a Stick.

FILES ONLY. This tool never flashes firmware, never changes boot settings (boot_option) or any NVS value (Wi-Fi), never writes or deletes
the device key, and never deletes or changes a file outside the install set. A complete, verified installation of the files does NOT
mean the assistant is ready: Wi-Fi, the device key, auto-start, the Mac server, the Claude login and an end-to-end voice check are
separate steps, reported at the end and not done here.

Writing uses the same staged, verified and resumable engine as tools/stick_restore.py (stage, verify on the Stick, activate, verify
every file, clean up). A multi-file installation is NOT atomic: an interrupted activation can leave old and new files mixed or one
missing; run `apply` again to finish. Tested only against a SIMULATED Stick (tests/test_stick_install.py).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stick_backup as sb  # noqa: E402
import stick_restore as sr  # noqa: E402

ROOT = sb.ROOT
MANIFEST = ROOT / "stick" / "install_manifest.json"
Refused, Failed = sb.Refused, sb.Failed
GENERATED = "generated:device-config"
NEVER = [
    "Firmware, bootloader, partition table: never flashed or written.",
    "NVS settings (Wi-Fi credentials, boot_option and other UIFlow settings): never written; auto-start is not switched on here.",
    "The device key (/flash/device_key.txt): never written, replaced or deleted.",
    "Files outside the install set (settings cache, user files, vendor files): never changed or deleted, not even to make room.",
]
LIMITS = [
    "A multi-file installation is not atomic: an interruption while files are being activated can leave old and new files mixed, or "
    "one missing. Running `apply` again finishes it; restarting before that may leave the app unable to start.",
    "Complete means every file in the install set matches it on the Stick by SHA-256. It does not mean the assistant is ready or "
    "that it starts.",
    "Tested only against a simulated Stick.",
]
REMAINING = ["Wi-Fi", "Device key", "Auto-start", "Mac server configuration", "Claude login", "End-to-end voice check"]


# ---- the install set (offline) --------------------------------------------------------------------------------------------------
def load_manifest():
    try:
        manifest = json.loads(MANIFEST.read_text())
    except (OSError, ValueError) as error:
        raise Refused(f"The install manifest {MANIFEST} cannot be read ({error}). Nothing was contacted.")
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise Refused("The install manifest has an unexpected shape. Nothing was contacted.")
    return manifest


def build_set(cfg):
    """(entries, notes): every file to install, its source checked against the pinned SHA-256. Raises Refused listing every problem."""
    manifest = load_manifest()
    problems, entries, seen = [], [], set()
    for item in manifest["files"]:
        path, source = item.get("path"), item.get("source")
        rel = sb.safe_relative(path) if isinstance(path, str) else None
        if rel is None or sr.protected(path):
            problems.append(f"{path!r}: unsafe or protected destination")
            continue
        if path in seen:
            problems.append(f"{path} is listed twice")
            continue
        seen.add(path)
        entry = {"path": path, "rel": rel, "role": item.get("role"), "note": item.get("note"), "license": item.get("license")}
        if source == GENERATED:
            try:
                text = json.dumps(sb.stick_config.device_config(cfg), indent=2, sort_keys=True) + "\n"
            except sb.stick_config.ConfigError as error:
                problems.append(f"{path}: the device configuration cannot be generated from this Mac's configuration ({str(error).splitlines()[0]})")
                continue
            data = text.encode()
            entry.update(source=GENERATED, data=data, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        else:
            local = (ROOT / str(source)) if isinstance(source, str) else None
            if local is None or not str(source).startswith("stick/") or ".." in Path(source).parts:
                problems.append(f"{path}: the source must be a file under stick/ in this checkout")
                continue
            if not local.exists():
                problems.append(f"{path}: MISSING source file {source}")
                continue
            if local.is_symlink() or not local.is_file():
                problems.append(f"{path}: {source} is not a regular file")
                continue
            digest, size = sr.file_sha(local)
            if digest != item.get("sha256"):
                problems.append(f"{path}: {source} does not match its pinned SHA-256 (changed since the install set was reviewed)")
                continue
            entry.update(source=source, local=local, size=size, sha256=digest)
        entries.append(entry)
    if problems:
        raise Refused("The install set is not ready. Nothing was contacted or written.\n" + "\n".join("  - " + p for p in problems))
    return manifest, entries


def staged_need(entries):
    """What staging these files needs on the Stick (the same rule the write engine applies)."""
    return sum(e["size"] for e in entries) + 8192 * len(entries) + 64 * 1024


def kit_order(entries):
    """Activation order = manifest order (configuration and sounds first, modules in deploy order, main.py last). Risk reduction only."""
    index = {item["path"]: i for i, item in enumerate(load_manifest()["files"])}
    return sorted(entries, key=lambda e: index.get(e["path"], len(index)))


def print_plan(manifest, entries, cfg=None):
    print("OFFLINE INSTALLATION PLAN: no Stick was contacted. This checks the install set; it is NOT a comparison with any device.")
    print(f"Install manifest: {MANIFEST.relative_to(ROOT)} ({len(entries)} files, {sum(e['size'] for e in entries)} bytes); every source matches its pinned SHA-256.")
    for e in entries:
        extra = f"   [{e['note']}]" if e.get("note") else ""
        print(f"  {e['role']:<22} {e['path']}  ({e['size']} bytes){extra}")
    print(f"\nSpace: a first installation stages all {len(entries)} files at once and needs about {staged_need(entries) // 1024} KB free on the Stick "
          "(sizes + 8 KB per file + 64 KB). On an existing installation only the files that differ are staged. Previous versions are moved, not "
          "copied. Nothing is ever deleted to make room: if it doesn't fit, nothing is written.")
    print("\nLICENSING: " + manifest["licensing"]["summary"])
    for e in entries:
        if e.get("license"):
            print(f"  {e['path']}: {e['license']}")
    print("Non-commercial use only: the downloaded Hebrew voice is CC-BY-NC-4.0 (THIRD_PARTY_NOTICES.md).")
    print("\nNever done by this tool:")
    for line in NEVER:
        print("  * " + line)
    print_remaining(None, cfg)
    print("\nNext: preflight --port <port> (connects to the Stick, read-only).")


def print_remaining(ident, cfg=None):
    """File installation is not 'assistant ready'. What is observed on the Stick is presence only and is not a check that it works."""
    def obs(key):
        value = None if ident is None else ident.get(key)
        return "unknown" if value is None else "yes" if value is True else "no" if value is False else str(value)
    print("\nREMAINING SETUP (not done or checked by this tool; the assistant is NOT ready until all of these are done):")
    print(f"  * Wi-Fi: saved on the Stick: network name {obs('wifi_ssid_saved')}, password {obs('wifi_password_saved')} (presence only; check it with tools/stick_setup.py wifi)")
    print(f"  * Device key: present on the Stick: {obs('device_key_present')}; provisioning and pairing with this Mac are not checked (tools/stick_setup.py key, then verify)")
    print(f"  * Auto-start: boot option {obs('boot_option')} (0 starts the assistant by itself); this tool never changes it (tools/stick_setup.py autostart)")
    target = f"{cfg.advertise_host}:{cfg.device_port}" if cfg is not None and cfg.advertise_host else "the configured address"
    print(f"  * Mac server configuration: the Stick is pointed at {target}; whether the server runs and is reachable there is not checked")
    print("  * Claude login on the Mac: not checked")
    print("  * End-to-end voice check (press, speak, hear the reply): not done")


# ---- the Stick ----------------------------------------------------------------------------------------------------------------
def check_device(tool, expect_device=None):
    ident = sb.identify(tool)
    sb.print_identity(tool.port, ident)
    if ident["device_id"] == "unknown" or ident["support"] == "unknown":
        raise Failed("Refused: the Stick could not be identified completely (not the supported UIFlow firmware?). Nothing was written.")
    if ident["support"] != "supported":
        raise Failed("Refused: the Stick's firmware is not the supported version; installing onto it is not supported, and this tool never "
                     "flashes firmware. Nothing was written.")
    if expect_device and ident["device_id"] != expect_device:
        raise Failed(f"Refused: this is device {ident['device_id']}, not {expect_device}. Nothing was written.")
    return ident


def install_state(result, entries):
    """'no assistant installed' / 'existing installation' / 'already installed', judged by the app modules (main.py and the vendor
    files a fresh UIFlow Stick also has are not evidence of an installation)."""
    modules = {e["path"] for e in entries if e["role"] == "module"}
    present = modules & {e["path"] for e in result["match"] + result["change"]}
    if not present:
        return "no assistant installed (first installation)"
    if not result["change"] and not result["missing"]:
        return "already installed: every file matches this install set"
    same = len(modules & {e["path"] for e in result["match"]})
    return f"existing installation ({len(present)} of {len(modules)} modules present, {same} identical to this install set)"


def print_space(result, free):
    todo = result["change"] + result["missing"]
    if not todo:
        return
    need = staged_need(todo)
    after = None if free is None else free - sum(e["size"] for e in result["missing"]) - sum(e["size"] - (e.get("device_size") or 0) for e in result["change"])
    print(f"\nSPACE: staging {len(todo)} file(s) needs about {need // 1024} KB free; the Stick has "
          + ("an unknown amount" if free is None else f"{free // 1024} KB")
          + ("" if after is None else f"; about {max(after, 0) // 1024} KB would remain after installation "
                                       "(file sizes only: block rounding is not counted, so somewhat less)") + ".")
    print("  The staging allowance is a fixed margin (8 KB per file + 64 KB), not computed from the Stick's block size; "
          "it is meant to cover block rounding, folder metadata and a reserve.")
    if free is not None and free < need:
        print("  NOT ENOUGH SPACE: nothing would be written, and no file is ever deleted to make room.")


def backup_check(args, ident, cfg):
    """The before-write rule: replacing an existing file needs a complete backup of THIS Stick holding the version now on it."""
    def check(result, todo):
        replacing = result["change"]
        if not replacing:
            return
        names = ", ".join(e["path"] for e in replacing)
        if not args.backup:
            raise Failed(f"Refused: this would replace {len(replacing)} existing file(s) on the Stick ({names}). Take a complete backup first "
                         "(tools/stick_backup.py backup --port <port>) and pass it with --backup <folder>. Nothing was written.")
        try:
            manifest, backup_entries, _ = sr.validate_backup(args.backup)
        except Refused as error:
            raise Failed("Refused: the backup given with --backup cannot be used.\n" + str(error))
        if manifest["device"]["device_id"] != ident["device_id"]:
            raise Failed(f"Refused: the backup is of device {manifest['device']['device_id']}, not this Stick ({ident['device_id']}). Nothing was written.")
        held = {e["path"]: (e["sha256"], e["size"]) for e in backup_entries}
        stale = [e["path"] for e in replacing if held.get(e["path"]) != (e["device_sha256"], e["device_size"])]
        if stale:
            raise Failed("Refused: the backup does not hold the version now on the Stick of: " + ", ".join(stale)
                         + ". Take a fresh backup. Nothing was written.")
        print(f"\nBackup checked: {args.backup} is complete and holds the current version of all {len(replacing)} file(s) to be replaced.")
        return manifest
    return check


def earlier_unfinished(device_dir, own):
    """Earlier installation runs on this Stick that did not complete, with what they created and replaced (for later recovery)."""
    found = []
    for path in sorted(device_dir.glob("*/record.json")):
        if path.parent.name == own:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("status") != "complete":
            files = data.get("files") or []
            found.append({"operation": data.get("operation"), "status": data.get("status"), "stage": data.get("stage"),
                          "created": [f["path"] for f in files if f.get("action") == "create"],
                          "replaced": [f["path"] for f in files if f.get("action") == "replace"]})
    return found


# ---- recovery: remove what an installation created ----------------------------------------------------------------------------
REMOVE_CREATED_SNIPPET = r'''
import os, hashlib, binascii, json
for p, want, n in FILES:
    try:
        st = os.stat(p)
    except OSError:
        print("STICKRM:" + json.dumps([p, "absent"]))
        continue
    if st[0] & 0x4000:
        print("STICKRM:" + json.dumps([p, "folder"]))
        continue
    h = hashlib.sha256()
    size = 0
    with open(p, "rb") as f:
        while True:
            b = f.read(1024)
            if not b:
                break
            size += len(b)
            h.update(b)
    if binascii.hexlify(h.digest()).decode() != want or size != n:
        print("STICKRM:" + json.dumps([p, "changed"]))
        continue
    os.remove(p)
    print("STICKRM:" + json.dumps([p, "removed"]))
print("STICKRM-END")
'''


def load_install_record(where):
    """The record.json of one installation (its operation folder or the file itself), and the files it recorded as created."""
    path = Path(where).expanduser()
    path = path / "record.json" if path.is_dir() else path
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise Refused(f"Refused: cannot read the installation record {path} ({error}). Nothing was contacted.")
    if not isinstance(data, dict) or data.get("kind") != "installation" or not isinstance(data.get("files"), list):
        raise Refused(f"Refused: {path} is not an installation record of this tool. Nothing was contacted.")
    device_id = (data.get("device") or {}).get("device_id")
    if not device_id:
        raise Refused(f"Refused: the record {path} does not name the Stick it was made on. Nothing was contacted.")
    created = []
    for f in data["files"]:
        if f.get("action") != "create":
            continue
        p = f.get("path")
        if (not isinstance(p, str) or not p.startswith("/flash/") or "/../" in p or p.endswith("/..") or "//" in p or sr.protected(p)
                or not isinstance(f.get("sha256"), str) or not isinstance(f.get("size"), int)):
            raise Refused(f"Refused: the record lists an unusable created file ({p!r}). Nothing was contacted.")
        created.append(f)
    return path, data, device_id, created


def remove_created(args, cfg):
    path, data, device_id, created = load_install_record(args.record)
    names = sorted(f["path"] for f in created)
    if not created:
        raise Refused(f"Refused: the record {path} lists no file as created by that installation, so there is nothing to remove.")
    if sorted(set(args.expect_remove or [])) != names:
        raise Refused("Refused: --expect-remove must name exactly the files this installation created ("
                      + ", ".join(names) + "); given: " + (", ".join(sorted(set(args.expect_remove or []))) or "none") + ". Nothing was contacted.")
    port = sb.choose_port(args.port)
    if not os.access(cfg.mpremote, os.X_OK):
        raise Refused(f"The Stick tool was not found: {cfg.mpremote}.")
    tool = sb.StickTool(cfg.mpremote, port)
    print("Note: " + sb.IMPACT.replace("Nothing is written to /flash or NVS.", "remove-created DELETES only the files listed below, "
                                       "and only if they are exactly what the installation wrote (never NVS).") + "\n")
    print(f"Installation record: {path} (operation {data.get('operation')}, status {data.get('status')})")
    try:
        check_device(tool, device_id)
        states, _, finished, why = sr.read_state(tool, names)
        if not finished:
            raise Failed(f"Could not read the files on the Stick ({why}). Nothing was removed.")
        remove, absent, problems = [], [], []
        for f in created:
            _, kind, size, digest = states[f["path"]]
            if kind == "none":
                absent.append(f["path"])
            elif kind == "f" and (digest, size) == (f["sha256"], f["size"]):
                remove.append(f)
            else:
                problems.append(f"{f['path']}: " + ("it is not the file this installation wrote (changed since)" if kind == "f"
                                                    else "not a plain file on the Stick"))
        print("\nFILES THIS INSTALLATION CREATED:")
        for f in remove:
            print(f"  {f['path']}: exactly as installed -> will be removed")
        for p in absent:
            print(f"  {p}: not on the Stick (nothing to remove)")
        if problems:
            for line in problems:
                print("  " + line)
            raise Failed("Refused: at least one created file is not exactly what the installation wrote, so NOTHING was removed. "
                         "Look at it yourself; this tool never deletes contents it cannot account for.")
        removed = []
        if remove:
            items, done, why = tool.exec_markers(sr.fill(REMOVE_CREATED_SNIPPET, files=[[f["path"], f["sha256"], f["size"]] for f in remove]),
                                                 "STICKRM", "STICKRM-END")
            removed = [i[0] for i in items if isinstance(i, list) and len(i) == 2 and i[1] == "removed"]
            if not done:
                print(f"  (the removal did not finish: {why})")
        states, _, finished, why = sr.read_state(tool, names)
        still = [p for p in names if not (finished and states[p][1] == "none")]
        data.setdefault("recovery", []).append({"action": "remove-created", "utc": sr.now(), "removed": removed, "already_absent": absent,
                                                 "still_present": still})
        tmp = path.parent / ".record.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
        if still:
            raise Failed("Removal could not be verified: still on the Stick (or unreadable): " + ", ".join(still) + ".")
        print("\nREMOVED AND VERIFIED ABSENT: " + (", ".join(removed) if removed else "nothing needed removing")
              + ". No other file was touched. The record notes this recovery step.")
        return 0
    finally:
        if tool.connected():
            print("The Stick was not restarted. Restart it only once every recovery step is done and verified:\n  "
                  + sr.RESTART_CMD.format(program=tool.program, port=port))


def kit_commit():
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "stick"], capture_output=True, text=True, timeout=10)
        return done.stdout.strip() + (" (stick/ has uncommitted changes)" if dirty.stdout.strip() else "") if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def preflight_json(tool, args, entries, cfg, port):
    """preflight --json: the same read-only checks; the readable report on stderr, one JSON object on stdout."""
    import contextlib
    out = {"read_only": True, "ok": False}
    try:
        with contextlib.redirect_stdout(sys.stderr):
            print("Note: " + sb.IMPACT + "\n")
            try:
                ident = check_device(tool, args.expect_device)
                result, free, extras, leftovers = sr.compare(tool, entries)
                sr.print_comparison(result, extras, leftovers, free, others="not part of the install set")
                todo = result["change"] + result["missing"]
                out.update(ok=True, device_id=ident["device_id"], state=install_state(result, entries),
                           complete=not (todo or result["conflict"]), replace=[e["path"] for e in result["change"]],
                           create=[e["path"] for e in result["missing"]], conflict=[e["path"] for e in result["conflict"]],
                           free_bytes=free, staging_bytes=staged_need(todo) if todo else 0,
                           enough_space=(free is not None and free >= staged_need(todo)) if todo else True,
                           backup_required=bool(result["change"]), leftover_staging=len(leftovers))
                print(f"\nINSTALLATION STATE: {out['state']}")
                print_space(result, free)
            finally:
                if tool.connected():
                    print("The assistant app (if any) is stopped until the Stick restarts:\n  " + sr.RESTART_CMD.format(program=tool.program, port=port))
    except (Refused, Failed) as error:
        out["error"] = str(error)
        print(str(error), file=sys.stderr)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["ok"] else 1


def pin():
    manifest = load_manifest()
    changed = []
    for item in manifest["files"]:
        if item.get("source") != GENERATED:
            digest = sr.file_sha(ROOT / item["source"])[0]
            if item.get("sha256") != digest:
                changed.append(item["path"])
                item["sha256"] = digest
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print("Re-pinned: " + (", ".join(changed) if changed else "nothing changed"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="stick_install.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="offline: check the install set (opens no port)")
    sub.add_parser("pin", help="maintainers: re-pin source hashes after a reviewed change")
    p = sub.add_parser("remove-created", help="recovery: remove only the files one installation created, if unchanged")
    p.add_argument("record", help="that installation's operation folder (or its record.json)")
    p.add_argument("--port", help="the Stick's serial port (required: never chosen automatically)")
    p.add_argument("--expect-remove", action="append", metavar="PATH", help="each file the installation created (all of them, exactly)")
    for name in ("preflight", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--port", help="the Stick's serial port (required: never chosen automatically)")
        p.add_argument("--expect-device", help="refuse unless the Stick reports this device id")
        if name == "preflight":
            p.add_argument("--json", action="store_true", help="print the findings as JSON on stdout (the readable report goes to stderr)")
        if name == "apply":
            p.add_argument("--backup", help="a complete backup of this Stick, required when existing files would be replaced")
            p.add_argument("--expect-changes", action="append", metavar="PATH", help="refuse unless exactly these files would be written")
            p.add_argument("--restart", action="store_true", help="restart the Stick at the end, only when that is safe")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.command == "pin":
        return pin()
    try:
        try:
            cfg = sb.stick_config.load(require_server=False)
        except sb.stick_config.ConfigError as error:
            raise Refused(f"Configuration problem: {error}")
        if args.command == "remove-created":                  # recovery must not depend on the current sources
            return remove_created(args, cfg)
        manifest, entries = build_set(cfg)
        if args.command == "plan":
            print_plan(manifest, entries, cfg)
            return 0
        port = sb.choose_port(args.port)
        if not os.access(cfg.mpremote, os.X_OK):
            raise Refused(f"The Stick tool was not found: {cfg.mpremote}.")
        tool = sb.StickTool(cfg.mpremote, port)
        if args.command == "preflight":
            if getattr(args, "json", False):
                return preflight_json(tool, args, entries, cfg, port)
            print("Note: " + sb.IMPACT + "\n")
            try:
                ident = check_device(tool, args.expect_device)
                result, free, extras, leftovers = sr.compare(tool, entries)
                sr.print_comparison(result, extras, leftovers, free, others="not part of the install set")
                print(f"\nINSTALLATION STATE: {install_state(result, entries)}")
                print_space(result, free)
                if result["conflict"]:
                    print("\nCONFLICTS: installing is refused until they are resolved by you; this tool never deletes or moves them.")
                if result["change"]:
                    print(f"\nBACKUP REQUIRED: {len(result['change'])} existing file(s) would be replaced; apply needs --backup with a complete, "
                          "fresh backup of this Stick (tools/stick_backup.py backup).")
                print_remaining(ident, cfg)
                print("\nNothing was written.")
                return 0
            finally:
                if tool.connected():
                    print("The assistant app (if any) is stopped until the Stick restarts:\n  " + sr.RESTART_CMD.format(program=tool.program, port=port))
        base = sr.record_base(cfg, "stick-installs")
        print("Note: " + sb.IMPACT.replace("Nothing is written to /flash or NVS.", "apply WRITES the install set's files that differ to /flash (never NVS).") + "\n")
        ident = None
        rec = None
        code = 1
        try:
            ident = check_device(tool, args.expect_device)
            rec = sr.Record(base, ident["device_id"], kind="installation", install_manifest={"path": str(MANIFEST.relative_to(ROOT)),
                            "sha256": sr.file_sha(MANIFEST)[0], "kit_commit": kit_commit()}, never=NEVER, limits=LIMITS,
                            remaining_setup=REMAINING, assistant_ready=False)
            rec.data["device"] = {k: v for k, v in ident.items() if k != "app"}
            rec.data["earlier_unfinished_installations"] = earlier_unfinished(rec.folder.parent, rec.op)
            print(f"Operation record: {rec.folder / 'record.json'}")
            for e in entries:                                          # the generated configuration is kept, privately, as this run's source
                if e["source"] == GENERATED:
                    e["local"] = rec.folder / e["rel"].replace("/", "_")
                    fd = os.open(e["local"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(e["data"])
            rec.data["device_config_sha256"] = next((e["sha256"] for e in entries if e["source"] == GENERATED), None)

            def revalidate(e):
                return (not e["local"].is_symlink()) and sr.file_sha(e["local"]) == (e["sha256"], e["size"])

            def before_write(result, todo):
                rec.data["installation_state_before"] = install_state(result, entries)
                print(f"\nINSTALLATION STATE: {rec.data['installation_state_before']}")
                checked = backup_check(args, ident, cfg)(result, todo)
                if checked:
                    rec.data["backup"] = {"folder": str(Path(args.backup).resolve()), "made_utc": checked.get("created_utc")}
            outcome = sr.write_files(tool, entries, rec, expect=args.expect_changes, revalidate=revalidate, before_write=before_write,
                                     order=kit_order, source="the install set", others_key="left_on_device_not_in_install_set",
                                     others="not part of the install set")
            if outcome == "nothing":
                rec.data["installation_state_before"] = install_state({"match": entries, "change": [], "missing": []}, entries)
                rec.save()
            print("\nFILES INSTALLED AND VERIFIED: " + rec.data["summary"] + ".")
            print("This is the file installation only. The assistant is NOT ready yet (see REMAINING SETUP).")
            code = 0
        except Failed as error:
            if rec:
                rec.finish("failed")
            where = f" at stage '{rec.data['stage']}'" if rec else ""
            print(f"\nINSTALLATION FAILED{where}: {error}", file=sys.stderr)
            for failure in (rec.data["failures"] if rec else []):
                print(f"  problem: {failure['path'] or '(installation)'}: {failure['reason']}", file=sys.stderr)
        except BaseException as error:                                  # Ctrl-C or an unexpected error: never "complete"
            if rec:
                rec.data["failures"].append({"path": None, "reason": "interrupted: " + type(error).__name__})
                rec.finish("interrupted")
            raise
        finally:
            safe = rec.data["safe_to_restart_app"] if rec else True
            if rec:
                print(sr.guidance(rec, tool, noun="installation"))
                for line in NEVER[3:] + LIMITS[:2]:
                    print("  * " + line)
            if ident is not None:
                print_remaining(ident, cfg)
            if args.restart and safe and tool.connected():
                tool.run(["exec", "--no-follow", "import machine; machine.reset()"], sb.EXEC_TIMEOUT_S)
                print("The Stick was asked to restart." + ("" if ident and ident.get("boot_option") == 0 else
                                                          " (Its boot option is not 0, so the assistant will not start by itself.)"))
            elif args.restart:
                print("The Stick was NOT restarted" + ("" if tool.connected() else " (it is not connected)") + ".")
            elif tool.connected() and safe:
                print("The app (if any) is stopped until the Stick restarts:\n  " + sr.RESTART_CMD.format(program=tool.program, port=port))
        return code
    except Refused as error:
        print(str(error), file=sys.stderr)
        return 2
    except Failed as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
