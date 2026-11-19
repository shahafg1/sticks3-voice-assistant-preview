"""Tests for tools/stick_restore.py (offline restore plan, device comparison, staged and verified file restore) against a SIMULATED Stick.
Run: python3 tests/test_stick_restore.py

Synthetic files only: each case builds a simulated Stick (tests/fixtures/fake_stick_device.py, made writable for these cases), takes a
backup of it with tools/stick_backup.py, changes the simulated Stick, and restores. The isolation guard is active in every process.
This checks the tool's logic and its device-side snippets run in CPython; it says nothing about MicroPython or a real Stick."""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import test_stick_backup as tb

ROOT = tb.ROOT
TOOL = ROOT / "tools" / "stick_restore.py"
KEY, CONTENT = tb.KEY, tb.CONTENT
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class Lab(tb.Lab):
    def __init__(self, **state):
        state.setdefault("writable", True)
        super().__init__(**state)
        (self.flash / "device_config.json").write_text(json.dumps({"server": "192.0.2.10", "port": 18000}))

    def restore(self, *args, timeout=240, **env):
        return self.run(*args, tool=TOOL, timeout=timeout, **env)

    def backup(self):
        done = self.run("backup", "--port", self.port)
        assert done.returncode == 0, done.stdout + done.stderr
        self.log.write_text("")
        return self.backups()[-1]

    def snapshot(self):
        return {str(p.relative_to(self.flash)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.flash.rglob("*") if p.is_file()}

    def app_files(self):
        return {k: v for k, v in self.snapshot().items() if not k.startswith(".stick-restore/")}

    def staging_files(self):
        root = self.flash / ".stick-restore"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []

    def damage(self):
        """Change one app module, remove one file, add a file the backup does not have."""
        with open(self.flash / "robot.py", "a") as handle:
            handle.write("\n# changed after the backup\n")
        (self.flash / "apps" / "demo.py").unlink()
        (self.flash / "extra.py").write_text("print('not in the backup')\n")

    def set(self, **changes):
        state = self.state()
        state.update(changes)
        self.state_path.write_text(json.dumps(state))

    def writes(self):
        return [c for c in self.calls() if c and c[0] in ("WRITE", "WRITE-REFUSED")]

    def records(self):
        root = self.data / "stick-restores"
        found = root.glob("stick-*/*/record.json") if root.exists() else []
        return sorted(found, key=lambda p: os.stat(p.parent).st_birthtime)          # names share a one-second stamp: order by creation

    def record(self, index=-1):
        return json.loads(self.records()[index].read_text())


def lab_case(fn=None, **kw):
    def wrap(f):
        def run():
            lab = Lab(**kw)
            try:
                f(lab)
            finally:
                lab.close()
        run.__name__ = f.__name__
        return case(run)
    return wrap(fn) if fn else wrap


def copy_backup(lab, folder, name):
    target = Path(lab.tmp.name) / "variants" / name
    shutil.copytree(folder, target, symlinks=True)
    return target


def edit_manifest(folder, change):
    m = json.loads((folder / "manifest.json").read_text())
    change(m)
    (folder / "manifest.json").write_text(json.dumps(m))


def no_leaks(lab, *texts):
    tb.no_secrets(*texts, lab.log.read_text())
    for text in texts:
        assert CONTENT not in text, "file content was printed"
    calls = lab.calls()
    assert not any(c and c[0] in ("KEY-WRITTEN-ON-DEVICE", "KEY-OPENED-ON-DEVICE") for c in calls), "the device key was touched"
    assert not any("device_key" in json.dumps(c) for c in calls if c and c[0] in ("WRITE", "WRITE-REFUSED"))


def assert_private(folder):
    for path in [folder, *[p for p in folder.rglob("*") if p.is_dir()]]:
        assert oct(path.stat().st_mode & 0o777) == "0o700", path
    for path in [p for p in folder.rglob("*") if p.is_file()]:
        assert oct(path.stat().st_mode & 0o777) == "0o600", path


# ---- offline plan --------------------------------------------------------------------------------------------------------------
@lab_case
def the_default_plan_is_offline_lists_files_and_exclusions_and_opens_no_port(lab):
    folder = lab.backup()
    done = lab.restore("plan", str(folder))
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "OFFLINE RESTORE PLAN" in out and "NOT a comparison" in out and "compare BACKUP --port" in out
    for record in lab.manifest(folder)["files"]:
        assert record["path"] in out, record["path"]
    assert "/flash/device_key.txt: excluded" in out and "never deleted" in out and "NVS settings" in out and "Firmware" in out
    as_json = json.loads(lab.restore("plan", str(folder), "--json").stdout)
    assert sorted(f["path"] for f in as_json["files"]) == sorted(r["path"] for r in lab.manifest(folder)["files"])
    assert as_json["mode"].startswith("offline")
    assert lab.restore("plan", str(folder), "--port", lab.port).returncode == 2           # plan has no port at all
    assert lab.calls() == [] and lab.records() == [], "the offline plan contacted the Stick or wrote a record"
    no_leaks(lab, out)


@lab_case
def compare_and_apply_need_an_explicit_port(lab):
    folder = lab.backup()
    for command in ("compare", "apply"):
        done = lab.restore(command, str(folder))
        assert done.returncode == 2 and "--port" in done.stderr, done.stderr
    assert lab.calls() == []


# ---- comparison --------------------------------------------------------------------------------------------------------------
@lab_case
def compare_reports_what_would_change_without_writing(lab):
    folder = lab.backup()
    lab.damage()
    before = lab.snapshot()
    done = lab.restore("compare", str(folder), "--port", lab.port)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "stops the running assistant app" in out and "Nothing was written" in out
    replaced = out.split("would be REPLACED")[1].split("would be CREATED")[0]
    created = out.split("would be CREATED")[1].split("already match")[0]
    assert "/flash/robot.py" in replaced and "/flash/apps/demo.py" not in replaced
    assert "/flash/apps/demo.py" in created
    n = len(lab.manifest(folder)["files"])
    assert f"already match (left alone): {n - 2}" in out
    assert "/flash/extra.py" in out.split("never deleted")[1]
    assert lab.writes() == [] and lab.snapshot() == before and lab.records() == []
    no_leaks(lab, out)


# ---- a successful restore ---------------------------------------------------------------------------------------------------------
@lab_case
def a_restore_stages_verifies_activates_and_reports_success_only_after_final_hashes(lab):
    original = lab.snapshot()
    folder = lab.backup()
    lab.damage()
    done = lab.restore("apply", str(folder), "--port", lab.port, "--restart")
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "RESTORE COMPLETE" in out and "2 file(s) written" in out, out
    after = lab.app_files()
    assert after.pop("extra.py") and after == original                 # restored exactly; the extra file is left in place
    assert (lab.flash / "device_key.txt").read_text() == KEY
    assert lab.staging_files() == [] and not (lab.flash / ".stick-restore").exists()     # staging removed, even its empty folder
    cps = [w for w in lab.writes() if w[1] == "cp"]
    assert sorted(w[2].split("/stage/")[1] for w in cps) == ["apps/demo.py", "robot.py"]      # unchanged files were not sent
    assert all("/.stick-restore/" in w[2] for w in cps)                  # every copy goes to the staging area first
    rec = lab.record()
    assert rec["status"] == "complete" and rec["safe_to_restart_app"] is True and rec["failures"] == []
    assert {f["path"]: f["action"] for f in rec["files"]} == {"/flash/robot.py": "replace", "/flash/apps/demo.py": "create"}
    assert all(f["staged_verified"] and f["activated"] and f["final_verified"] for f in rec["files"])
    assert [s["stage"] for s in rec["stages"]] == ["identify", "compare", "space", "revalidate", "prepare", "staging", "verify-staged",
                                                   "activating", "verify-final", "cleanup"]
    assert rec["left_on_device_not_in_backup"] == ["/flash/extra.py"] and rec["never"]
    assert_private(lab.records()[-1].parent)
    assert CONTENT not in lab.records()[-1].read_text()
    assert lab.state()["nvs"]["ssid0"] == tb.SSID and lab.state().get("resets") == 1
    no_leaks(lab, out)


@lab_case
def expected_changes_must_match_exactly_or_nothing_is_written(lab):
    folder = lab.backup()
    lab.damage()                                                         # robot.py differs and apps/demo.py is missing
    for expect in (["/flash/robot.py"], ["/flash/robot.py", "/flash/apps/demo.py", "/flash/main.py"], []):
        args = [a for path in expect for a in ("--expect-changes", path)] or ["--expect-changes", "/flash/none.py"]
        done = lab.restore("apply", str(folder), "--port", lab.port, *args)
        assert done.returncode == 1 and "not exactly the expected ones" in done.stderr, done.stderr
    assert lab.writes() == []
    done = lab.restore("apply", str(folder), "--port", lab.port, "--expect-changes", "/flash/apps/demo.py", "--expect-changes", "/flash/robot.py")
    assert done.returncode == 0 and "RESTORE COMPLETE" in done.stdout, done.stdout + done.stderr


@lab_case
def files_that_already_match_are_not_written(lab):
    folder = lab.backup()
    done = lab.restore("apply", str(folder), "--port", lab.port)
    assert done.returncode == 0 and "nothing to write" in done.stdout, done.stdout + done.stderr
    assert lab.writes() == [] and lab.record()["status"] == "complete"


# ---- backups that are refused before contacting the Stick ------------------------------------------------------------------------
@lab_case
def incomplete_corrupt_or_altered_backups_are_refused_offline(lab):
    folder = lab.backup()
    def variant(name, change):
        target = copy_backup(lab, folder, name)
        change(target)
        return target
    variants = {
        "incomplete": lambda f: edit_manifest(f, lambda m: m.update(status="incomplete")),
        "killed": lambda f: edit_manifest(f, lambda m: (m.update(status="in-progress"), m.pop("finished_utc"))),
        "failures": lambda f: edit_manifest(f, lambda m: m.update(failures=[{"path": "/flash/robot.py", "reason": "copy failed"}])),
        "unverified": lambda f: edit_manifest(f, lambda m: m["files"][0].update(verified=False)),
        "altered-copy": lambda f: open(f / "files" / "robot.py", "a").write("#"),
        "truncated-copy": lambda f: (f / "files" / "robot.py").write_bytes((f / "files" / "robot.py").read_bytes()[:10]),
        "missing-copy": lambda f: (f / "files" / "robot.py").unlink(),
        "bad-manifest": lambda f: (f / "manifest.json").write_text("{not json"),
        "unlisted-file": lambda f: (f / "files" / "smuggled.py").write_text("print(1)\n"),
        "unsupported-firmware": lambda f: edit_manifest(f, lambda m: m["device"].update(uiflow="V2.4.9", support="unsupported")),
        "no-files": lambda f: edit_manifest(f, lambda m: m.update(files=[])),
    }
    for name, change in variants.items():
        target = variant(name, change)
        for command in (("plan", str(target)), ("apply", str(target), "--port", lab.port)):
            done = lab.restore(*command)
            assert done.returncode == 2 and ("cannot be restored" in done.stderr or "damaged" in done.stderr), (name, command[0], done.stderr)
            assert "RESTORE COMPLETE" not in done.stdout
    assert lab.calls() == [] and lab.records() == [], "a refused backup still contacted the Stick or started a record"


@lab_case
def unsafe_paths_symlinks_and_the_staging_area_are_refused(lab):
    folder = lab.backup()
    outside = Path(lab.tmp.name) / "outside"
    outside.mkdir()
    def symlinked_file(f):
        shutil.copy2(f / "files" / "robot.py", outside / "robot.py")       # identical bytes: only the link itself is wrong
        (f / "files" / "robot.py").unlink()
        (f / "files" / "robot.py").symlink_to(outside / "robot.py")
    def symlinked_folder(f):
        shutil.copytree(f / "files" / "apps", outside / "apps")
        shutil.rmtree(f / "files" / "apps")
        (f / "files" / "apps").symlink_to(outside / "apps")
    def escaping_path(f):
        edit_manifest(f, lambda m: m["files"][0].update(path="/flash/../etc/robot.py"))
    def staging_area(f):
        def change(m):
            m["files"][0]["path"] = "/flash/.stick-restore/x/robot.py"
        edit_manifest(f, change)
    for name, change in (("file-link", symlinked_file), ("folder-link", symlinked_folder), ("escape", escaping_path), ("staging", staging_area)):
        target = copy_backup(lab, folder, name)
        change(target)
        done = lab.restore("apply", str(target), "--port", lab.port)
        assert done.returncode == 2 and "cannot be restored" in done.stderr, (name, done.stderr)
        assert {"file-link": "symbolic link", "folder-link": "symbolic link", "escape": "unsafe", "staging": "protected"}[name] in done.stderr, name
    link = Path(lab.tmp.name) / "backup-link"
    link.symlink_to(folder)
    assert lab.restore("plan", str(link)).returncode == 2
    assert lab.calls() == []


@lab_case
def a_backup_that_contains_a_device_key_is_refused(lab):
    folder = lab.backup()
    stand_in = "synthetic-not-a-key"
    def listed(f):
        (f / "files" / "device_key.txt").write_text(stand_in)
        digest = hashlib.sha256(stand_in.encode()).hexdigest()
        edit_manifest(f, lambda m: m["files"].append({"path": "/flash/device_key.txt", "size": len(stand_in), "sha256": digest,
                                                       "device_sha256": digest, "listed_size": len(stand_in), "verified": True}))
    def unlisted(f):
        (f / "files" / "apps" / "Device_Key.TXT").write_text(stand_in)
    for name, change in (("listed", listed), ("unlisted", unlisted)):
        target = copy_backup(lab, folder, name)
        change(target)
        for command in (("plan", str(target)), ("apply", str(target), "--port", lab.port)):
            done = lab.restore(*command)
            assert done.returncode == 2 and "device key" in done.stderr, (name, done.stderr)
            assert stand_in not in done.stdout + done.stderr
    assert lab.calls() == [] and (lab.flash / "device_key.txt").read_text() == KEY


# ---- refused at the Stick, before any write -----------------------------------------------------------------------------------------
@lab_case
def another_stick_is_refused_before_any_write(lab):
    folder = lab.backup()
    lab.set(unique_id="0f0e0d0c0b0a")
    done = lab.restore("apply", str(folder), "--port", lab.port)
    assert done.returncode == 1 and "belongs to device" in done.stderr, done.stderr
    rec = lab.record()
    assert lab.writes() == [] and rec["status"] == "failed" and rec["stage"] == "identify" and rec["safe_to_restart_app"] is True


@lab_case
def unsupported_firmware_on_the_stick_is_refused_before_any_write(lab):
    folder = lab.backup()
    lab.set(firmware_info=["x", "y", "z", "V2.4.9"])
    for command in ("compare", "apply"):
        done = lab.restore(command, str(folder), "--port", lab.port)
        assert done.returncode == 1 and "not the supported version" in done.stderr, done.stderr
    assert lab.writes() == []


@lab_case
def insufficient_space_on_the_stick_is_refused_before_any_write(lab):
    folder = lab.backup()
    lab.damage()
    lab.set(flash_free=8192)
    done = lab.restore("apply", str(folder), "--port", lab.port)
    assert done.returncode == 1 and "KB free" in done.stderr and "Nothing was written" in done.stderr, done.stderr
    assert lab.writes() == [] and lab.record()["stage"] == "space"


@lab_case
def a_folder_where_a_file_belongs_is_a_conflict_and_nothing_is_written(lab):
    folder = lab.backup()
    (lab.flash / "robot.py").unlink()
    (lab.flash / "robot.py").mkdir()
    done = lab.restore("apply", str(folder), "--port", lab.port)
    assert done.returncode == 1 and "CONFLICTS" in done.stdout and "a folder has this name" in done.stdout, done.stdout
    assert lab.writes() == []


# ---- interruption and rerun -----------------------------------------------------------------------------------------------------
@lab_case
def interrupted_transfer_leaves_app_files_untouched_and_a_rerun_finishes(lab):
    original = lab.snapshot()
    folder = lab.backup()
    lab.damage()
    damaged = lab.app_files()
    lab.set(faults={"cp_to_fail": ["/flash/robot.py"]})
    done = lab.restore("apply", str(folder), "--port", lab.port, "--restart")
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "RESTORE FAILED at stage 'staging'" in out and "RESTORE COMPLETE" not in out, out
    assert "safe to restart" in out and "no app file was changed" in out
    assert lab.app_files() == damaged and lab.staging_files()           # targets untouched; a staged copy is left over
    first = lab.record()
    assert first["status"] == "failed" and first["safe_to_restart_app"] is True and lab.state().get("resets") == 1
    first_text = lab.records()[0].read_text()
    lab.set(faults={})
    done = lab.restore("apply", str(folder), "--port", lab.port)
    assert done.returncode == 0 and "RESTORE COMPLETE" in done.stdout, done.stdout + done.stderr
    after = lab.app_files()
    after.pop("extra.py")
    assert after == original and lab.staging_files() == []              # the stale staged copy was cleared, nothing else left
    assert len(lab.records()) == 2 and lab.records()[0].read_text() == first_text      # the earlier record is never rewritten


@lab_case
def a_disconnect_during_transfer_lists_what_was_not_attempted(lab):
    folder = lab.backup()
    lab.damage()
    damaged = lab.app_files()
    lab.set(calls=0, faults={"disconnect_after_calls": 5})             # identify (2), compare (2), prepare (1); then the copies
    done = lab.restore("apply", str(folder), "--port", lab.port)
    assert done.returncode == 1 and "RESTORE COMPLETE" not in done.stdout
    rec = lab.record()
    assert rec["stage"] == "staging" and any("not attempted" in f["reason"] for f in rec["failures"]) and rec["safe_to_restart_app"]
    assert lab.app_files() == damaged
    Path(lab.port).touch()                                               # plugged back in
    lab.set(faults={})
    assert lab.restore("apply", str(folder), "--port", lab.port).returncode == 0


@lab_case
def a_damaged_staged_copy_stops_before_activation(lab):
    folder = lab.backup()
    lab.damage()
    damaged = lab.app_files()
    lab.set(faults={"cp_to_truncate": ["/flash/robot.py"]})
    done = lab.restore("apply", str(folder), "--port", lab.port)
    assert done.returncode == 1 and "stage 'verify-staged'" in done.stderr and "RESTORE COMPLETE" not in done.stdout
    assert lab.app_files() == damaged and not any(w[1] == "rename" for w in lab.writes())


@lab_case
def interrupted_activation_says_do_not_restart_and_a_rerun_finishes(lab):
    original = lab.snapshot()
    folder = lab.backup()
    lab.damage()
    damaged_robot = (lab.flash / "robot.py").read_bytes()
    lab.set(faults={"activation_interrupt_after_renames": 2})          # demo.py created (1), robot.py moved aside (2): then cut off
    done = lab.restore("apply", str(folder), "--port", lab.port, "--restart")
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "stage 'activating'" in out and "RESTORE COMPLETE" not in out, out
    assert "Do NOT restart" in out and "NOT restarted" in out and lab.state().get("resets", 0) == 0
    assert not (lab.flash / "robot.py").exists()                         # the dangerous state: a module is missing
    rec = lab.record()
    assert rec["safe_to_restart_app"] is False and rec["status"] == "failed"
    prev = lab.flash / ".stick-restore" / rec["operation"] / "prev" / "robot.py"
    assert prev.read_bytes() == damaged_robot                            # the previous version is kept on the Stick
    lab.set(faults={})
    done = lab.restore("apply", str(folder), "--port", lab.port, "--restart")
    assert done.returncode == 0 and "RESTORE COMPLETE" in done.stdout and lab.state()["resets"] == 1, done.stdout + done.stderr
    after = lab.app_files()
    after.pop("extra.py")
    assert after == original
    second = lab.record()
    assert second["plan"]["missing"] == ["/flash/robot.py"] and "/flash/apps/demo.py" in second["plan"]["match"]
    assert second["earlier_previous_versions_kept"] == [f"/flash/.stick-restore/{rec['operation']}/prev/robot.py"]
    assert prev.exists()                                                 # never deleted automatically: it is not this run's
    assert [p.name for p in (lab.flash / ".stick-restore").iterdir()] == [rec["operation"]]   # the area stays while it holds that


@lab_case
def a_run_killed_during_activation_is_never_marked_complete_or_safe(lab):
    folder = lab.backup()
    lab.damage()
    lab.set(faults={"hang_on_exec_marker": "STICKACT"})
    proc = subprocess.Popen([sys.executable, str(TOOL), "apply", str(folder), "--port", lab.port], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=lab.env(), stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 90
        while time.time() < deadline and not (lab.records() and lab.record()["stage"] == "activating"):
            time.sleep(0.2)
        time.sleep(0.5)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    rec = lab.record()
    assert rec["status"] == "in-progress" and rec["stage"] == "activating" and rec["safe_to_restart_app"] is False
    lab.set(faults={})
    assert lab.restore("apply", str(folder), "--port", lab.port).returncode == 0


@lab_case
def a_file_that_does_not_verify_after_activation_is_a_failure(lab):
    folder = lab.backup()
    lab.damage()
    lab.set(faults={"corrupt_after_activation": ["/flash/robot.py"]})
    done = lab.restore("apply", str(folder), "--port", lab.port, "--restart")
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "stage 'verify-final'" in out and "RESTORE COMPLETE" not in out, out
    rec = lab.record()
    assert rec["status"] == "failed" and rec["safe_to_restart_app"] is False and lab.state().get("resets", 0) == 0
    assert [f["final_verified"] for f in rec["files"] if f["path"] == "/flash/robot.py"] == [False]


# ---- units ----------------------------------------------------------------------------------------------------------------------
spec = tb.importlib.util.spec_from_file_location("stick_restore_under_test", TOOL)
sr = tb.importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


@case
def the_device_snippets_print_only_markers_and_delete_only_in_the_staging_area():
    for snippet in (sr.STATE_SNIPPET, sr.PREPARE_SNIPPET, sr.ACTIVATE_SNIPPET, sr.CLEANUP_SNIPPET):
        prints = [line.strip() for line in snippet.splitlines() if "print(" in line]
        assert prints and all(line.startswith('print("STICK') for line in prints), prints
        assert "device_key" not in snippet and "NVS" not in snippet
    assert 'AREA = "/flash/.stick-restore/"' in sr._HELPERS and "outside the restore staging area" in sr._HELPERS
    for snippet in (sr.PREPARE_SNIPPET, sr.ACTIVATE_SNIPPET, sr.CLEANUP_SNIPPET):
        own = snippet.replace(sr._HELPERS, "")
        assert "os.remove(" not in own
        assert own.count("os.rmdir(") == (1 if snippet is sr.CLEANUP_SNIPPET else 0)
    assert "if isdir(AREA[:-1]) and not list(os.ilistdir(AREA[:-1])):\n    os.rmdir(AREA[:-1])" in sr.CLEANUP_SNIPPET   # only when empty


@case
def a_source_path_through_a_symbolic_link_is_refused_on_its_own():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store, outside = Path(tmp) / "files", Path(tmp) / "outside"
        (store / "apps").mkdir(parents=True)
        (outside / "apps").mkdir(parents=True)
        (store / "ok.py").write_text("x")
        (outside / "apps" / "a.py").write_text("x")
        (store / "link.py").symlink_to(outside / "apps" / "a.py")
        (store / "dirlink").symlink_to(outside / "apps")
        assert sr.safe_local(store, "ok.py") == (store / "ok.py", None)
        for rel in ("link.py", "dirlink/a.py"):
            assert sr.safe_local(store, rel)[1] == "a symbolic link in the backup folder (refused)", rel
        assert sr.safe_local(store, "apps")[1] == "not a regular file in the backup folder"
        assert sr.safe_local(store, "absent.py")[1] == "missing from the backup folder"


@case
def protected_names_are_never_restorable():
    for path in ("/flash/device_key.txt", "/flash/apps/DEVICE_KEY.bak", "/flash/.stick-restore", "/flash/.stick-restore/a/b"):
        assert sr.protected(path), path
    for path in ("/flash/robot.py", "/flash/device_config.json", "/flash/.stick-restored.py"):
        assert not sr.protected(path), path


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:600])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:600])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
