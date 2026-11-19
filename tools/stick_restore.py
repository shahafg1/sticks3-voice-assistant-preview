#!/usr/bin/env python3
"""Plan, compare and (only when asked) apply a FILE restore from a backup made by tools/stick_backup.py (docs/stick-restore.md).

    python3 tools/stick_restore.py plan    BACKUP                          OFFLINE: check the backup and list what it holds; opens no port
    python3 tools/stick_restore.py compare BACKUP --port /dev/cu.usbmodemXXXX     read-only: identify the Stick, compare every file
    python3 tools/stick_restore.py apply   BACKUP --port /dev/cu.usbmodemXXXX     write the files that differ or are missing
          [--restart]   restart the Stick at the end, only if that is safe (see below)
          [--expect-changes PATH]   refuse before any write unless exactly these files would be written (repeatable)
          [--only PATH]             compare/apply: only these backup files (repeatable); every other file is left as it is
    BACKUP is one backup folder: <data_dir>/stick-backups/stick-<id>/<time>-<random>

WHAT IT NEVER DOES: write, restore or delete the device key, the NVS settings (Wi-Fi, boot_option) or the firmware; delete a file
that is not in the backup (those stay, so this is NOT an exact rollback of the whole Stick); restore an incomplete, altered or
unverified backup, a backup of another Stick, or onto firmware other than the supported version.

HOW `apply` WRITES (each step is recorded before it starts, in a private record under <data_dir>/stick-restores):
  1. check the backup again: every file's size and SHA-256 against the manifest, before anything is written;
  2. identify the Stick (same device id as the backup, supported firmware), compare every file, check free space on the Stick;
  3. copy the files that differ or are missing into /flash/.stick-restore/<operation>/stage/ and verify each copy ON THE STICK;
     until here no file the app uses has been touched;
  4. activate: for each file, move the current version (if any) to /flash/.stick-restore/<operation>/prev/, then move the staged
     copy into place. This is the only step that changes the app's files. It is NOT atomic: an interruption here can leave a mix of
     old and new files, or a file missing (its previous version is then in prev/). The order (other files first, the app modules in
     deploy order, boot.py and main.py last) only reduces risk; no mix is claimed to start;
  5. verify every file in the backup on the Stick by SHA-256; only then is the restore reported complete;
  6. remove /flash/.stick-restore/<operation>/ (the staged copies and the replaced previous versions), and the empty
     /flash/.stick-restore folder itself when nothing else (such as an earlier run's kept previous versions) is in it.
  Interrupted? Run `apply` again: it compares by hash, skips files that already match, and finishes the rest. Do not restart the
  Stick after an interruption in steps 4-5 until a rerun has completed (the app may not start). See docs/stick-restore.md.

CONNECTING (compare, apply) stops the running assistant app and leaves the Stick at the MicroPython REPL, like stick_backup.py.
Tested against a SIMULATED Stick (tests/test_stick_restore.py). On real hardware, only one single-file restore of a file the app
does not use has been run (docs/stick-restore.md); multi-file restores and interruptions are simulated only.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stick_backup as sb  # noqa: E402

TOOL_VERSION = 1
STAGE_ROOT = "/flash/.stick-restore"
NEVER = [
    "The device key (/flash/device_key.txt): never in a backup, never written, restored or deleted.",
    "NVS settings (Wi-Fi credentials, boot_option and other UIFlow settings): not part of a file backup, never written.",
    "Firmware, bootloader and partition table: not part of a file backup, never written.",
    "Files on the Stick that are not in the backup: left in place, never deleted. This is not an exact rollback of the whole Stick.",
]
LIMITS = [
    "A multi-file restore is not atomic: an interruption while files are being activated can leave old and new files mixed, or a "
    "file missing. Rerunning `apply` finishes it; restarting before that may leave the app unable to start.",
    "A complete restore means every file in the backup matches it on the Stick by SHA-256. It does not prove that the app starts, "
    "and it does not restore anything outside these files.",
    "Tested against a simulated Stick; on hardware only one single-file restore of a non-app file (docs/stick-restore.md).",
]
SAFE_STAGES = {"validate", "identify", "compare", "space", "revalidate", "prepare", "staging", "verify-staged", "cleanup"}
RESTART_CMD = '{program} connect {port} resume exec --no-follow "import machine; machine.reset()"'

# ---- device-side snippets (MicroPython). Markers only: names, kinds, sizes and hashes, never file contents.
STATE_SNIPPET = r'''
import os, hashlib, binascii, json
def kind(p):
    try:
        return "d" if os.stat(p)[0] & 0x4000 else "f"
    except OSError:
        return None
for p in PATHS:
    try:
        k = kind(p)
        if k == "f":
            h = hashlib.sha256()
            n = 0
            with open(p, "rb") as f:
                while True:
                    b = f.read(1024)
                    if not b:
                        break
                    n += len(b)
                    h.update(b)
            print("STICKSTAT:" + json.dumps([p, "f", n, binascii.hexlify(h.digest()).decode()]))
        elif k == "d":
            print("STICKSTAT:" + json.dumps([p, "d", None, None]))
        else:
            a = p.rsplit("/", 1)[0]
            blocked = False
            while a and a != "/flash":
                if kind(a) == "f":
                    blocked = True
                a = a.rsplit("/", 1)[0]
            print("STICKSTAT:" + json.dumps([p, "blocked" if blocked else "none", None, None]))
    except Exception:
        print("STICKSTAT:" + json.dumps([p, "error", None, None]))
try:
    s = os.statvfs("/flash")
    print("STICKFREE:" + json.dumps([s[1], s[4]]))
except Exception:
    print("STICKFREE:" + json.dumps(None))
print("STICKSTAT-END")
'''

# Shared by the writing snippets: rm() refuses anything outside the staging area; mk() never turns a file into a folder.
_HELPERS = r'''
import os, json
AREA = "/flash/.stick-restore/"
def isdir(p):
    try:
        return bool(os.stat(p)[0] & 0x4000)
    except OSError:
        return None
def rm(p):
    if not p.startswith(AREA) or "/../" in p or p.endswith("/.."):
        raise ValueError("outside the restore staging area")
    d = isdir(p)
    if d:
        for e in list(os.ilistdir(p)):
            rm(p + "/" + e[0])
        os.rmdir(p)
    elif d is not None:
        os.remove(p)
def mk(p):
    cur = ""
    for x in p.split("/")[1:]:
        cur += "/" + x
        d = isdir(cur)
        if d is None:
            os.mkdir(cur)
        elif not d:
            raise OSError(20)
'''

PREPARE_SNIPPET = _HELPERS + r'''
for p in REMOVE:
    rm(p)
    print("STICKPREP:" + json.dumps(["removed", p]))
for p in DIRS:
    mk(p)
print("STICKPREP-END")
'''

ACTIVATE_SNIPPET = _HELPERS + r'''
for t, s, p in OPS:
    mk(t.rsplit("/", 1)[0])
    d = isdir(t)
    if d:
        raise OSError(21)
    if d is not None:
        mk(p.rsplit("/", 1)[0])
        os.rename(t, p)
        print("STICKACT:" + json.dumps([t, "previous-moved"]))
    os.rename(s, t)
    print("STICKACT:" + json.dumps([t, "activated"]))
print("STICKACT-END")
'''

CLEANUP_SNIPPET = _HELPERS + r'''
for p in REMOVE:
    rm(p)
if isdir(AREA[:-1]) and not list(os.ilistdir(AREA[:-1])):
    os.rmdir(AREA[:-1])
print("STICKCLEAN-END")
'''


def fill(snippet, **values):
    """Prepend the snippet's inputs as literals (JSON lists of strings are valid MicroPython)."""
    return "".join(f"{name.upper()} = {json.dumps(value)}\n" for name, value in values.items()) + snippet


Refused, Failed = sb.Refused, sb.Failed


# ---- the backup (offline) ----------------------------------------------------------------------------------------------------
def protected(path):
    """Never restored: the device key (under any spelling of its name) and the restore staging area itself."""
    name = path.rsplit("/", 1)[-1].lower()
    return path == sb.KEY_FILE or name.startswith("device_key") or path == STAGE_ROOT or path.startswith(STAGE_ROOT + "/")


def file_sha(path):
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            h.update(block)
            n += len(block)
    return h.hexdigest(), n


def safe_local(store, rel):
    """(path, None) for a regular file reached without any symbolic link, else (None, reason)."""
    current = store
    for i, part in enumerate(rel.split("/")):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            return None, "missing from the backup folder"
        if stat.S_ISLNK(mode):
            return None, "a symbolic link in the backup folder (refused)"
        last = i == len(rel.split("/")) - 1
        if last and not stat.S_ISREG(mode):
            return None, "not a regular file in the backup folder"
        if not last and not stat.S_ISDIR(mode):
            return None, "not reachable through folders in the backup folder"
    return current, None


def validate_backup(folder):
    """(manifest, entries, warnings). Raises Refused, listing every problem, unless the backup can be restored as it is."""
    folder = Path(folder).expanduser()
    try:
        mode = os.lstat(folder).st_mode
    except OSError:
        raise Refused(f"{folder} does not exist.")
    if not stat.S_ISDIR(mode):
        raise Refused(f"{folder} is not a backup folder (a symbolic link or a file is refused).")
    manifest_path = folder / "manifest.json"
    try:
        mmode = os.lstat(manifest_path).st_mode
    except OSError:
        raise Refused(f"No manifest.json in {folder}: not a backup made by stick_backup.py, or it is damaged.")
    if not stat.S_ISREG(mmode):
        raise Refused("manifest.json is not a regular file (refused).")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (ValueError, UnicodeDecodeError):
        raise Refused("manifest.json cannot be read: the backup is damaged.")
    if not isinstance(manifest, dict):
        raise Refused("manifest.json has an unexpected shape: the backup is damaged.")
    problems, warnings = [], []
    if mode & 0o077 or mmode & 0o077:
        warnings.append("the backup folder or its manifest can be read by other users of this Mac")
    if manifest.get("tool_version") != 1:
        problems.append(f"made by an unknown backup tool version ({manifest.get('tool_version')!r})")
    if manifest.get("status") != "complete":
        problems.append(f"the backup is not complete (status {manifest.get('status')!r}); an incomplete or interrupted backup is never restored")
    if manifest.get("failures"):
        problems.append(f"the backup recorded {len(manifest['failures'])} failure(s)")
    if not manifest.get("finished_utc"):
        problems.append("the backup never finished normally (no finish time)")
    device = manifest.get("device") if isinstance(manifest.get("device"), dict) else {}
    if not (isinstance(device.get("device_id"), str) and re.fullmatch(r"[0-9a-f]{12}", device["device_id"])):
        problems.append("the backup names no valid device id")
    s = sb.SUPPORTED
    if not (device.get("support") == "supported" and device.get("uiflow") == s["uiflow"] and device.get("micropython") == s["micropython"]
            and s["build_date"] in str(device.get("micropython_build")) and s["model"] in str(device.get("model"))):
        problems.append("the backup was not taken from the supported firmware (only that is restored)")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        problems.append("the backup lists no files")
        records = []
    entries, seen = [], set()
    for record in records:
        path = record.get("path") if isinstance(record, dict) else None
        rel = sb.safe_relative(path) if isinstance(path, str) else None
        if rel is None:
            problems.append(f"unsafe or malformed path {path!r} (refused)")
            continue
        if protected(path):
            problems.append(f"{path} is protected (the device key or the restore staging area) and is never restored")
            continue
        if path in seen:
            problems.append(f"{path} is listed twice")
            continue
        seen.add(path)
        size, digest = record.get("size"), record.get("sha256")
        if not (isinstance(size, int) and size >= 0 and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            problems.append(f"{path}: size or hash missing")
            continue
        if record.get("verified") is not True or record.get("device_sha256") != digest:
            problems.append(f"{path}: was not verified against the Stick when the backup was made")
            continue
        local, why = safe_local(folder / "files", rel)
        if why:
            problems.append(f"{path}: {why}")
            continue
        if file_sha(local) != (digest, size):
            problems.append(f"{path}: the stored copy does not match the manifest (damaged or altered)")
            continue
        entries.append({"path": path, "rel": rel, "size": size, "sha256": digest, "local": local})
    store = folder / "files"
    listed = {sb.safe_relative(r["path"]) for r in records if isinstance(r, dict) and isinstance(r.get("path"), str)}
    if os.path.lexists(store) and not os.path.isdir(store) or os.path.islink(store):
        problems.append("files/ is not a plain folder (refused)")
    else:
        for dirpath, dirnames, filenames in os.walk(store):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                rel = p.relative_to(store).as_posix()
                if name.lower().startswith("device_key"):
                    problems.append(f"files/{rel}: a device key file must never be in a backup; this backup is refused")
                elif p.is_symlink():
                    problems.append(f"files/{rel} is a symbolic link (refused)")
                elif p.is_file() and rel not in listed:
                    problems.append(f"files/{rel} is not listed in the manifest (the backup was altered)")
    if problems:
        raise Refused("This backup cannot be restored. Nothing was contacted or written.\n" + "\n".join("  - " + p for p in problems))
    return manifest, entries, warnings


def activation_order(entries):
    """Risk reduction only: other files first, then the app modules in deploy order, boot.py and main.py last."""
    kit = list(sb.kit_app_files())
    def key(e):
        if e["path"] == "/flash/main.py":
            return (3, 0, "")
        if e["path"] == "/flash/boot.py":
            return (2, 0, "")
        if e["path"] in kit:
            return (1, kit.index(e["path"]), "")
        return (0, 0, e["path"])
    return sorted(entries, key=key)


def print_plan(folder, manifest, entries, warnings):
    print("OFFLINE RESTORE PLAN: no Stick was contacted. This only checks the backup; it is NOT a comparison with any device.")
    print(f"Backup        {folder}")
    print(f"  made        {manifest.get('created_utc')} from device {manifest['device']['device_id']} "
          f"(UIFlow {manifest['device']['uiflow']}, MicroPython {manifest['device']['micropython']})")
    print(f"  status      complete; every stored file matches the manifest by size and SHA-256")
    for warning in warnings:
        print(f"  warning     {warning}")
    print(f"\nFiles the backup would restore if they differ on the Stick or are missing ({len(entries)}, "
          f"{sum(e['size'] for e in entries)} bytes):")
    for e in sorted(entries, key=lambda e: e["path"]):
        print(f"  {e['path']}  ({e['size']} bytes)")
    print("\nNot in this backup, never restored:")
    for item in manifest.get("excluded") or []:
        print(f"  {item.get('path')}: excluded when the backup was made")
    for item in manifest.get("skipped") or []:
        print(f"  {item.get('path')}: skipped when the backup was made")
    for line in NEVER:
        print("  * " + line)
    print("\nWhich files would actually change is only known after: compare BACKUP --port <port> (connects to the Stick, read-only).")


# ---- the Stick ----------------------------------------------------------------------------------------------------------------
def read_state(tool, paths):
    """{path: [path, kind, size, sha]} and the free bytes on /flash (or None), from one read-only snippet."""
    status, out, err = tool.run(["exec", fill(STATE_SNIPPET, paths=paths)], sb.EXEC_TIMEOUT_S + sum(1 for _ in paths))
    states, free = {}, None
    for line in out.splitlines():
        if line.startswith("STICKSTAT:"):
            try:
                item = json.loads(line[len("STICKSTAT:"):])
            except ValueError:
                continue
            if isinstance(item, list) and len(item) == 4:
                states[item[0]] = item
        elif line.startswith("STICKFREE:"):
            try:
                v = json.loads(line[len("STICKFREE:"):])
                free = v[0] * v[1] if isinstance(v, list) and len(v) == 2 else None
            except (ValueError, TypeError):
                free = None
    finished = status == 0 and "STICKSTAT-END" in out.splitlines() and all(p in states for p in paths)
    return states, free, finished, (err.strip().splitlines() or ["no answer"])[-1][:200]


def check_device(tool, manifest):
    ident = sb.identify(tool)
    sb.print_identity(tool.port, ident)
    want = manifest["device"]["device_id"]
    if ident["device_id"] == "unknown" or ident["support"] == "unknown":
        raise Failed("Refused: the Stick could not be identified completely. Nothing was written.")
    if ident["device_id"] != want:
        raise Failed(f"Refused: this backup belongs to device {want}, but the connected Stick is {ident['device_id']}. Nothing was written.")
    if ident["support"] != "supported":
        raise Failed("Refused: the Stick's firmware is not the supported version, so restoring files onto it is not supported. Nothing was written.")
    return ident


def compare(tool, entries):
    """Classify each file against the Stick: match / change / missing, or a conflict that blocks applying. A "change" entry also
    carries the Stick's current hash and size (device_sha256, device_size)."""
    states, free, finished, why = read_state(tool, [e["path"] for e in entries])
    if not finished:
        raise Failed(f"Could not read every file on the Stick ({why}). Nothing was written.")
    result = {"match": [], "change": [], "missing": [], "conflict": []}
    for e in entries:
        _, kind, size, digest = states[e["path"]]
        if kind == "f":
            if (digest, size) == (e["sha256"], e["size"]):
                result["match"].append(e)
            else:
                result["change"].append(dict(e, device_sha256=digest, device_size=size))
        elif kind == "none":
            result["missing"].append(e)
        else:
            result["conflict"].append(dict(e, why={"d": "a folder has this name on the Stick", "blocked": "a file on the Stick blocks its folder",
                                                   "error": "could not be read on the Stick"}.get(kind, kind)))
    entries_listed, done, why = tool.exec_markers(sb.LIST_SNIPPET, "STICKLS", "STICKLS-END")
    if not done:
        raise Failed(f"Could not list the files on the Stick ({why}). Nothing was written.")
    in_backup = {e["path"] for e in entries}
    extras, leftovers = [], []
    for item in entries_listed:
        if not (isinstance(item, list) and len(item) == 3) or item[1] != "f":
            continue
        if item[0].startswith(STAGE_ROOT + "/"):
            leftovers.append({"path": item[0], "size": item[2]})
        elif item[0] not in in_backup and not sb.key_like(item[0]):
            extras.append(item[0])
    return result, free, sorted(extras), leftovers


def print_comparison(result, extras, leftovers, free, others="not in the backup"):
    print("\nCOMPARISON WITH THE STICK (read-only):")
    for label, title in (("change", "would be REPLACED (different on the Stick)"), ("missing", "would be CREATED (missing on the Stick)"),
                         ("match", "already match (left alone)")):
        print(f"  {title}: {len(result[label])}")
        if label != "match":
            for e in result[label]:
                print(f"    {e['path']}")
    if result["conflict"]:
        print(f"  CONFLICTS (applying is refused): {len(result['conflict'])}")
        for e in result["conflict"]:
            print(f"    {e['path']}: {e['why']}")
    print(f"  on the Stick but {others} (left in place, never deleted): {len(extras)}")
    for path in extras:
        print(f"    {path}")
    print("  the device key, NVS settings and firmware: never touched")
    if leftovers:
        print(f"  left over from an earlier interrupted restore or installation in {STAGE_ROOT}: {len(leftovers)} file(s) "
              f"({sum(x['size'] or 0 for x in leftovers)} bytes)")
    if free is not None:
        print(f"  free space on the Stick: {free // 1024} KB")


# ---- the operation record -------------------------------------------------------------------------------------------------------
def record_base(cfg, name="stick-restores"):
    base = Path(cfg.data_dir) / name
    inside = str(base.resolve()).startswith(str(sb.ROOT.resolve()) + os.sep)
    if inside and (sb.ROOT / ".git").exists():
        import subprocess
        if subprocess.run(["git", "-C", str(sb.ROOT), "check-ignore", "-q", str(base / "x")], capture_output=True).returncode != 0:
            raise Refused(f"The record folder {base} would be inside the project and NOT ignored by git. Nothing was contacted.")
    return base


class Record:
    """The private operation record: names, sizes, hashes and stages only, never file contents. Written before each stage starts."""

    def __init__(self, base, device_id, **fields):
        self.folder = sb.new_backup_dir(sb.private_dir(sb.private_dir(base) / ("stick-" + device_id)))
        self.op = self.folder.name
        self.data = {"tool_version": TOOL_VERSION, "operation": self.op, "status": "in-progress", "stage": "validate",
                     "safe_to_restart_app": True, "created_utc": now(), **fields,
                     "device_staging": f"{STAGE_ROOT}/{self.op}", "stages": [], "files": [], "failures": [], "never": NEVER,
                     "limits": LIMITS, "connection": sb.IMPACT}
        self.data.update({k: v for k, v in fields.items() if k in ("never", "limits")})   # a caller's own lists (installation, setup) win
        self.save()

    def stage(self, name, **extra):
        self.data["stage"] = name
        self.data["safe_to_restart_app"] = name in SAFE_STAGES
        self.data["stages"].append({"stage": name, "started_utc": now()})
        self.data.update(extra)
        self.save()

    def finish(self, status, **extra):
        self.data.update(extra, status=status, finished_utc=now())
        if status == "complete":
            self.data["safe_to_restart_app"] = True
        self.save()

    def save(self):
        tmp = self.folder / ".record.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.folder / "record.json")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- apply --------------------------------------------------------------------------------------------------------------------
def select_only(entries, only):
    """--only: restore just these backup files. Every name must be a file this backup holds; anything else refuses the run."""
    by_path = {e["path"]: e for e in entries}
    unknown = sorted(set(only) - set(by_path))
    if unknown:
        raise Refused("Refused: --only names file(s) this backup does not hold: " + ", ".join(unknown) + ". Nothing was contacted.")
    return [by_path[p] for p in sorted(set(only))]


ONLY_OTHERS = "not selected with --only, or not in the backup"


def do_apply(tool, cfg, folder, manifest, entries, rec, expect=None, others="not in the backup"):
    rec.stage("identify")
    ident = check_device(tool, manifest)
    rec.data["device"] = {k: v for k, v in ident.items() if k != "app"}

    def revalidate(e):
        local, why = safe_local(folder / "files", e["rel"])
        return not why and file_sha(local) == (e["sha256"], e["size"])
    return write_files(tool, entries, rec, expect=expect, revalidate=revalidate, others=others)


def write_files(tool, entries, rec, expect=None, revalidate=None, before_write=None, order=None, source="the backup",
                others_key="left_on_device_not_in_backup", others="not in the backup"):
    """The shared write engine (restore and installation): compare, check space, revalidate the sources, stage, verify the staged
    copies on the Stick, activate, verify every file in `entries`, clean up. Each stage is recorded before it starts.
    Returns "nothing" or "written"; raises Failed (the record says at which stage, and whether a restart is safe)."""
    rec.stage("compare")
    result, free, extras, leftovers = compare(tool, entries)
    print_comparison(result, extras, leftovers, free, others=others)
    rec.data["plan"] = {k: [e["path"] for e in v] for k, v in result.items()}
    rec.data[others_key] = extras
    rec.data["earlier_previous_versions_kept"] = [x["path"] for x in leftovers if "/prev/" in x["path"]]
    if result["conflict"]:
        raise Failed("Refused: some files cannot be written as they are on the Stick (see CONFLICTS). Nothing was written.")
    todo = (order or activation_order)(result["change"] + result["missing"])
    if expect is not None and sorted(e["path"] for e in todo) != sorted(expect):
        raise Failed("Refused: the files that would be written are not exactly the expected ones (--expect-changes "
                     + (", ".join(sorted(expect)) or "none") + "; would write: " + (", ".join(sorted(e["path"] for e in todo)) or "none")
                     + "). Nothing was written.")
    if not todo:
        rec.finish("complete", summary=f"nothing to write: all {len(entries)} files already match {source}")
        return "nothing"
    if before_write:
        before_write(result, todo)

    rec.stage("space")
    stale = sorted({x["path"].split("/")[3] for x in leftovers})
    stale_stage = [f"{STAGE_ROOT}/{op}/stage" for op in stale]
    reclaim = sum(x["size"] or 0 for x in leftovers if "/stage/" in x["path"])
    need = sum(e["size"] for e in todo) + 8192 * len(todo) + 64 * 1024
    if free is None:
        raise Failed("Refused: the free space on the Stick could not be read. Nothing was written.")
    if free + reclaim < need:
        raise Failed(f"Refused: the Stick needs about {need // 1024} KB free for the staged copies and has {free // 1024} KB. Nothing was written.")
    if sb.free_bytes(rec.folder) < 1024 * 1024:
        raise Failed("Refused: less than 1 MB is free on this Mac for the operation record. Nothing was written.")

    rec.stage("revalidate")                                                      # every source file checked again just before the first write
    for e in todo:
        if not revalidate(e):
            raise Failed(f"Refused: the source of {e['path']} changed since it was checked. Nothing was written.")

    op_dir = f"{STAGE_ROOT}/{rec.op}"
    stage_of = {e["path"]: f"{op_dir}/stage/{e['rel']}" for e in todo}
    prev_of = {e["path"]: f"{op_dir}/prev/{e['rel']}" for e in todo}
    replacing = {e["path"] for e in result["change"]}
    rec.data["files"] = [{"path": e["path"], "action": "replace" if e["path"] in replacing else "create", "size": e["size"],
                          "sha256": e["sha256"], "staged": False, "staged_verified": False, "activated": False, "final_verified": False}
                         for e in todo]
    by_path = {f["path"]: f for f in rec.data["files"]}

    rec.stage("prepare", removed_stale_staging=stale_stage)
    dirs = sorted({s.rsplit("/", 1)[0] for s in stage_of.values()})
    _, done, why = tool.exec_markers(fill(PREPARE_SNIPPET, remove=stale_stage, dirs=dirs), "STICKPREP", "STICKPREP-END")
    if not done:
        raise Failed(f"Could not prepare the staging folder on the Stick ({why}). No app file was changed.")

    rec.stage("staging")
    print(f"\nStaging {len(todo)} file(s) in {op_dir} (the app's files are not touched yet)")
    for index, e in enumerate(todo):
        status, _, err = tool.run(["fs", "--no-verbose", "cp", str(e["local"]), ":" + stage_of[e["path"]]],
                                  sb.COPY_TIMEOUT_S + e["size"] // 2000)
        if status != 0:
            rec.data["failures"].append({"path": e["path"], "reason": "copy to the Stick failed" if status is not None else "copy timed out"})
            if not tool.connected():
                rec.data["failures"] += [{"path": x["path"], "reason": "not attempted: the Stick disconnected"} for x in todo[index + 1:]]
            raise Failed("Staging failed. No app file was changed.")
        by_path[e["path"]]["staged"] = True
        rec.save()

    rec.stage("verify-staged")
    states, _, finished, why = read_state(tool, list(stage_of.values()))
    for e in todo:
        st = states.get(stage_of[e["path"]])
        if st and st[1] == "f" and (st[3], st[2]) == (e["sha256"], e["size"]):
            by_path[e["path"]]["staged_verified"] = True
        else:
            rec.data["failures"].append({"path": e["path"], "reason": f"the staged copy on the Stick does not match {source}"})
    if rec.data["failures"]:
        raise Failed("A staged copy did not verify on the Stick. No app file was changed.")

    rec.stage("activating")
    ops = [[e["path"], stage_of[e["path"]], prev_of[e["path"]]] for e in todo]
    status, out, err = tool.run(["exec", fill(ACTIVATE_SNIPPET, ops=ops)], sb.EXEC_TIMEOUT_S)
    for line in out.splitlines():
        if line.startswith("STICKACT:"):
            try:
                path, what = json.loads(line[len("STICKACT:"):])
            except ValueError:
                continue
            if what == "activated" and path in by_path:
                by_path[path]["activated"] = True
    rec.save()
    if status != 0 or "STICKACT-END" not in out.splitlines():
        rec.data["failures"].append({"path": None, "reason": "activation was interrupted: "
                                     + (err.strip().splitlines() or ["no answer"])[-1][:200]})
        raise Failed("Activation was interrupted part-way.")

    rec.stage("verify-final")
    states, _, finished, why = read_state(tool, [e["path"] for e in entries])
    bad = []
    for e in entries:
        st = states.get(e["path"])
        ok = bool(st and st[1] == "f" and (st[3], st[2]) == (e["sha256"], e["size"]))
        if e["path"] in by_path:
            by_path[e["path"]]["final_verified"] = ok
        if not ok:
            bad.append(e["path"])
    if bad or not finished:
        rec.data["failures"] += [{"path": p, "reason": f"does not match {source} on the Stick after activation"} for p in bad]
        if not finished:
            rec.data["failures"].append({"path": None, "reason": f"final verification could not read every file ({why})"})
        raise Failed("Final verification failed.")

    rec.stage("cleanup")
    _, cleaned, why = tool.exec_markers(fill(CLEANUP_SNIPPET, remove=[op_dir]), "STICKCLEAN", "STICKCLEAN-END")
    leftover = [] if cleaned else [op_dir]
    rec.finish("complete", leftover_staging=leftover,
               summary=f"{len(todo)} file(s) written and all {len(entries)} files verified against {source}")
    return "written"


def guidance(rec, tool, noun="restore"):
    if rec.data["safe_to_restart_app"]:
        return ("It is safe to restart the Stick: "
                + (f"the {noun} finished and was verified." if rec.data["status"] == "complete" else "no app file was changed."))
    return ("Do NOT restart the Stick yet: the app's files may be a mix of old and new, or one may be missing, so the app may not start. "
            f"Run the same apply command again to finish the {noun} (it skips files that already match). "
            f"Previous versions of replaced files are in {rec.data['device_staging']}/prev on the Stick.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="stick_restore.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="offline: check the backup and list what it would restore (opens no port)")
    p.add_argument("backup")
    p.add_argument("--json", action="store_true")
    for name in ("compare", "apply"):
        p = sub.add_parser(name)
        p.add_argument("backup")
        p.add_argument("--port", help="the Stick's serial port (required: never chosen automatically)")
        p.add_argument("--only", action="append", metavar="PATH",
                       help="restore (or compare) only this backup file, e.g. /flash/voice_send.py; repeat for more. Other files are left as they are")
        if name == "apply":
            p.add_argument("--restart", action="store_true", help="restart the Stick at the end, only when that is safe")
            p.add_argument("--expect-changes", action="append", metavar="PATH",
                           help="refuse before any write unless exactly these files (e.g. /flash/README.md) would be written; repeat for more")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        folder = Path(args.backup).expanduser()
        manifest, entries, warnings = validate_backup(folder)
        only = getattr(args, "only", None)
        others = ONLY_OTHERS if only else "not in the backup"
        if only:
            entries = select_only(entries, only)
        if args.command == "plan":
            if args.json:
                print(json.dumps({"mode": "offline plan (no device contacted)", "backup": str(folder), "device_id": manifest["device"]["device_id"],
                                  "files": [{"path": e["path"], "size": e["size"], "sha256": e["sha256"]} for e in entries],
                                  "excluded": manifest.get("excluded") or [], "skipped": manifest.get("skipped") or [],
                                  "never": NEVER, "warnings": warnings}, indent=2, sort_keys=True))
            else:
                print_plan(folder, manifest, entries, warnings)
            return 0
        port = sb.choose_port(args.port)
        try:
            cfg = sb.stick_config.load(require_server=False)
        except sb.stick_config.ConfigError as error:
            raise Refused(f"Configuration problem: {error}")
        if not os.access(cfg.mpremote, os.X_OK):
            raise Refused(f"The Stick tool was not found: {cfg.mpremote}.")
        tool = sb.StickTool(cfg.mpremote, port)
        if args.command == "compare":
            print("Note: " + sb.IMPACT + "\n")
            try:
                check_device(tool, manifest)
                result, free, extras, leftovers = compare(tool, entries)
                print_comparison(result, extras, leftovers, free, others=others)
                print("\nNothing was written. " + ("Applying is refused while conflicts remain." if result["conflict"] else
                                                   "To write these files: apply BACKUP --port <port>."))
                return 0
            finally:
                if tool.connected():
                    print("The assistant app is stopped until the Stick restarts:\n  " + RESTART_CMD.format(program=tool.program, port=port))
        base = record_base(cfg)
        manifest_sha = file_sha(folder / "manifest.json")[0]
        rec = Record(base, manifest["device"]["device_id"], backup={"folder": str(folder.resolve()), "manifest_sha256": manifest_sha},
                     only=sorted(set(only)) if only else None)
        print("Note: " + sb.IMPACT.replace("Nothing is written to /flash or NVS.", "apply WRITES the files that differ to /flash (never NVS).") + "\n")
        print(f"Operation record: {rec.folder / 'record.json'}")
        code = 1
        try:
            outcome = do_apply(tool, cfg, folder, manifest, entries, rec, expect=args.expect_changes, others=others)
            print("\nRESTORE COMPLETE: " + rec.data["summary"] + ".")
            if outcome == "written" and rec.data.get("leftover_staging"):
                print(f"  (the staging folder {rec.data['leftover_staging'][0]} could not be removed; it is not used by the app)")
            code = 0
        except Failed as error:
            rec.finish("failed")
            print(f"\nRESTORE FAILED at stage '{rec.data['stage']}': {error}", file=sys.stderr)
            for failure in rec.data["failures"]:
                print(f"  problem: {failure['path'] or '(restore)'}: {failure['reason']}", file=sys.stderr)
        except BaseException as error:                                          # Ctrl-C or an unexpected error: never "complete"
            rec.data["failures"].append({"path": None, "reason": "interrupted: " + type(error).__name__})
            rec.finish("interrupted")
            raise
        finally:
            print(guidance(rec, tool))
            for line in NEVER[3:] + LIMITS[:2]:
                print("  * " + line)
            if args.restart and rec.data["safe_to_restart_app"] and tool.connected():
                tool.run(["exec", "--no-follow", "import machine; machine.reset()"], sb.EXEC_TIMEOUT_S)
                print("The Stick was asked to restart.")
            elif args.restart:
                print("The Stick was NOT restarted" + ("" if tool.connected() else " (it is not connected)") + ".")
            elif tool.connected() and rec.data["safe_to_restart_app"]:
                print("The assistant app is stopped until the Stick restarts:\n  " + RESTART_CMD.format(program=tool.program, port=port))
        return code
    except Refused as error:
        print(str(error), file=sys.stderr)
        return 2
    except Failed as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
