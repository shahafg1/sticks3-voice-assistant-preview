"""Tests for tools/stick_install.py (first-install file deployment) against a SIMULATED Stick.
Run: python3 tests/test_stick_install.py

Synthetic devices only: each case builds a simulated StickS3 (tests/fixtures/fake_stick_device.py, writable for these cases) as a fresh
UIFlow Stick, a current installation, or an OLD installation made from the v0.11.0 tag's files (with the pre-fix microphone code), and
installs this checkout's install set. Backups are made of the same simulated Stick with tools/stick_backup.py. The isolation guard is
active in every process. This checks the tool's logic and its device-side snippets run in CPython, not MicroPython or a real Stick."""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import test_stick_backup as tb

ROOT = tb.ROOT
TOOL = ROOT / "tools" / "stick_install.py"
MANIFEST = json.loads((ROOT / "stick" / "install_manifest.json").read_text())
KEY, CONTENT = tb.KEY, tb.CONTENT
SERVER = {"STICK_ADVERTISE_HOST": "192.0.2.10", "STICK_DEVICE_PORT": "50001", "STICK_PANEL_PORT": "50002"}
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git_show(ref, path):
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{path}"], capture_output=True, check=True).stdout


def expected_device_config():
    return (json.dumps({"v": tb.sb.stick_config.DEVICE_CONFIG_VERSION, "server_host": SERVER["STICK_ADVERTISE_HOST"],
                        "server_port": int(SERVER["STICK_DEVICE_PORT"])}, indent=2, sort_keys=True) + "\n").encode()


def install_set():
    """{device path: expected bytes} for this checkout's install set."""
    out = {}
    for item in MANIFEST["files"]:
        out[item["path"]] = expected_device_config() if item["source"].startswith("generated") else (ROOT / item["source"]).read_bytes()
    return out


REPLACED = ["/flash/voice_send.py", "/flash/device.py", "/flash/notify.py"]
CREATED = ["/flash/device_config.json", "/flash/server_config.py"]


class Lab(tb.Lab):
    """A simulated StickS3 on the supported firmware in one of these states:
    fresh   - UIFlow's own files only (boot.py, main.py, README.md, apps/), no assistant
    bare    - like fresh, without UIFlow's main.py (nothing the installer would replace)
    current - this install set already installed, plus a device key and a settings cache
    previous - an earlier installation, built from this checkout alone (no history): the install set with older versions of
               voice_send.py, device.py and notify.py and without server_config.py and device_config.json, plus a key, a settings
               cache and an unrelated user file
    old      - (development history only: tests/test_history_stick_install.py) the v0.11.0 tag's files"""

    def __init__(self, scenario="fresh", **state):
        state.setdefault("writable", True)
        super().__init__(app=False, **state)
        shutil.rmtree(self.flash)
        f = self.flash
        (f / "apps").mkdir(parents=True)
        (f / "boot.py").write_text("# vendor boot file stand-in\n")
        (f / "README.md").write_text("# vendor readme stand-in\n")
        (f / "apps" / "helloworld.py").write_text("print('hello')\n")
        if scenario != "bare":
            (f / "main.py").write_text("# vendor main.py stand-in\n")
        if scenario == "current":
            for path, data in install_set().items():
                (f / path[7:]).parent.mkdir(parents=True, exist_ok=True)
                (f / path[7:]).write_bytes(data)
        if scenario == "previous":
            for path, data in install_set().items():
                if path in CREATED:
                    continue
                (f / path[7:]).parent.mkdir(parents=True, exist_ok=True)
                (f / path[7:]).write_bytes(data + b"\n# an earlier version of this file\n" if path in REPLACED else data)
        if scenario == "old":
            tag = "v0.11.0"
            for name in subprocess.run(["git", "-C", str(ROOT), "ls-tree", "--name-only", tag, "stick/"], capture_output=True, text=True,
                                       check=True).stdout.split():
                if name.endswith(".py"):
                    (f / Path(name).name).write_bytes(git_show(tag, name))
            (f / "sounds").mkdir()
            for wav in sorted((ROOT / "stick" / "sounds").glob("*.wav")):
                shutil.copy2(wav, f / "sounds" / wav.name)
        if scenario in ("fresh", "bare"):
            self.set(nvs={"boot_option": 1})                                 # UIFlow's default: the menu; no Wi-Fi saved yet
        if scenario == "previous":
            (f / "voice_test.py").write_text("# the owner's own file stand-in\n")
        if scenario in ("current", "old", "previous"):
            (f / "device_key.txt").write_text(KEY)
            (f / "device_cache.json").write_text(json.dumps({"note": CONTENT}))

    def env(self, **more):
        return super().env(**{**SERVER, **more})

    def install(self, *args, timeout=300, **env):
        return self.run(*args, tool=TOOL, timeout=timeout, **env)

    def backup(self):
        done = self.run("backup", "--port", self.port)
        assert done.returncode == 0, done.stdout + done.stderr
        self.log.write_text("")
        return self.backups()[-1]

    def files(self):
        return {"/flash/" + str(p.relative_to(self.flash)): p.read_bytes() for p in self.flash.rglob("*") if p.is_file()
                and ".stick-restore" not in p.parts}

    def set(self, **changes):
        state = self.state()
        state.update(changes)
        self.state_path.write_text(json.dumps(state))

    def writes(self):
        return [c for c in self.calls() if c and c[0] in ("WRITE", "WRITE-REFUSED")]

    def records(self):
        root = self.data / "stick-installs"
        found = root.glob("stick-*/*/record.json") if root.exists() else []
        return sorted(found, key=lambda p: os.stat(p.parent).st_birthtime)

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


def assert_installed(lab, before):
    """Every install-set file is exactly this checkout's version; every other file is untouched."""
    after = lab.files()
    for path, data in install_set().items():
        assert after.get(path) == data, path
    for path, data in before.items():
        if path not in install_set():
            assert after.get(path) == data, ("a file outside the install set changed", path)
    assert set(after) == set(before) | set(install_set()), "a file outside the install set appeared or disappeared"


def assert_no_leaks(lab, *texts):
    tb.no_secrets(*texts, lab.log.read_text())
    for text in texts:
        assert CONTENT not in text
    calls = lab.calls()
    assert not any(c and c[0] in ("KEY-WRITTEN-ON-DEVICE", "KEY-OPENED-ON-DEVICE") for c in calls), "the device key was touched"
    assert not any("device_key" in json.dumps(c) or "device_cache" in json.dumps(c) for c in calls if c and c[0] in ("WRITE", "WRITE-REFUSED"))


def not_ready(out):
    assert "NOT ready" in out and "REMAINING SETUP" in out, out[-1500:]
    for item in ("Wi-Fi", "Device key", "Auto-start", "Mac server configuration", "Claude login", "End-to-end voice check"):
        assert item in out, item
    for line in out.lower().splitlines():                                 # "ready" only ever appears negated
        if re.search(r"\bready\b", line):
            assert "not" in line, ("readiness claimed", line)


# ---- the install set ------------------------------------------------------------------------------------------------------------
@case
def the_manifest_covers_every_module_main_the_configuration_and_every_sound_with_pinned_hashes():
    deploy = (ROOT / "tools" / "deploy_stick.sh").read_text().split("\nFILES=(")[1].split(")")[0].split()
    paths = [f["path"] for f in MANIFEST["files"]]
    assert [p[7:] for p in paths if p.endswith(".py")] == deploy and paths[-1] == "/flash/main.py"      # same modules, same order
    assert "/flash/device_config.json" in paths and paths.index("/flash/device_config.json") == 0
    sounds = sorted("/flash/sounds/" + p.name for p in (ROOT / "stick" / "sounds").glob("*.wav"))
    assert sounds and sorted(p for p in paths if p.startswith("/flash/sounds/")) == sounds
    for item in MANIFEST["files"]:
        if not item["source"].startswith("generated"):
            assert sha((ROOT / item["source"]).read_bytes()) == item["sha256"], ("pinned hash out of date: run stick_install.py pin", item["path"])
        if item["role"] == "sound":
            assert item["license"].startswith("original synthesized sound effect"), item     # no voice output is shipped
    assert "CC-BY-NC-4.0" in MANIFEST["licensing"]["summary"] and "not cleared for commercial use" in MANIFEST["licensing"]["summary"]


@case
def the_install_set_carries_the_microphone_module_and_statick():
    assert "from mic import Recorder" in (ROOT / "stick" / "voice_send.py").read_text()
    paths = {f["path"] for f in MANIFEST["files"]}
    assert {"/flash/mic.py", "/flash/statick_shapes.py", "/flash/robot.py"} <= paths


@case
def the_shipped_shake_sounds_are_exactly_what_the_original_generator_makes():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_sound_effects.py"), tmp], check=True, capture_output=True, timeout=120)
        for name in ("shake1.wav", "shake2.wav", "shake3.wav"):
            assert (Path(tmp) / name).read_bytes() == (ROOT / "stick" / "sounds" / name).read_bytes(), name



@lab_case
def the_default_plan_is_offline_and_shows_licensing_and_what_remains(lab):
    done = lab.install("plan")
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "OFFLINE INSTALLATION PLAN" in out and "NOT a comparison" in out and "Non-commercial use only" in out
    for item in MANIFEST["files"]:
        assert item["path"] in out, item["path"]
    assert "CC-BY-NC-4.0" in out and "microphone buffer-lifetime fix" in out
    not_ready(out)
    assert lab.install("plan", "--port", lab.port).returncode == 2                     # plan has no port at all
    assert lab.calls() == [] and lab.records() == []


@case
def a_missing_asset_a_changed_source_or_no_server_address_stops_before_any_device():
    lab = Lab()
    try:
        project = Path(lab.tmp.name) / "kit copy"
        for rel in ["tools/stick_install.py", "tools/stick_restore.py", "tools/stick_backup.py", "tools/deploy_stick.sh", "stick_config.py",
                    "stick/install_manifest.json", *[f["source"] for f in MANIFEST["files"] if not f["source"].startswith("generated")]]:
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, project / rel)
        tool = project / "tools" / "stick_install.py"

        def run(*args, **env):
            return subprocess.run([sys.executable, str(tool), *args], capture_output=True, text=True, env=lab.env(**env),
                                  stdin=subprocess.DEVNULL, timeout=120)
        assert run("plan").returncode == 0
        (project / "stick" / "sounds" / "shake2.wav").unlink()
        for command in (["plan"], ["apply", "--port", lab.port]):
            done = run(*command)
            assert done.returncode == 2 and "MISSING source file stick/sounds/shake2.wav" in done.stderr, done.stderr
        shutil.copy2(ROOT / "stick" / "sounds" / "shake2.wav", project / "stick" / "sounds" / "shake2.wav")
        with open(project / "stick" / "robot.py", "a") as handle:
            handle.write("# unreviewed change\n")
        done = run("preflight", "--port", lab.port)
        assert done.returncode == 2 and "does not match its pinned SHA-256" in done.stderr, done.stderr
        shutil.copy2(ROOT / "stick" / "robot.py", project / "stick" / "robot.py")
        done = run("plan", STICK_ADVERTISE_HOST="")
        assert done.returncode == 2 and "device configuration cannot be generated" in done.stderr, done.stderr
        assert lab.calls() == []
    finally:
        lab.close()


@lab_case
def preflight_and_apply_need_an_explicit_port(lab):
    for command in ("preflight", "apply"):
        done = lab.install(command)
        assert done.returncode == 2 and "--port" in done.stderr
    assert lab.calls() == []


# ---- preflight ------------------------------------------------------------------------------------------------------------------
@lab_case
def preflight_on_a_fresh_stick_reports_a_first_installation_and_writes_nothing(lab):
    before = lab.files()
    done = lab.install("preflight", "--port", lab.port)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "stops the running assistant app" in out and "INSTALLATION STATE: no assistant installed (first installation)" in out
    created = out.split("would be CREATED")[1].split("already match")[0]
    assert all(p in created for p in install_set() if p != "/flash/main.py")
    assert "/flash/main.py" in out.split("would be REPLACED")[1].split("would be CREATED")[0]       # UIFlow's own main.py
    assert "BACKUP REQUIRED" in out and "SPACE: staging 15 file(s)" in out
    assert "block rounding is not counted" in out and "not computed from the Stick's block size" in out, out
    for path in ("/flash/boot.py", "/flash/README.md", "/flash/apps/helloworld.py"):
        assert path in out.split("not part of the install set")[1]
    not_ready(out)
    assert lab.writes() == [] and lab.files() == before and lab.records() == []



@lab_case
def unsupported_or_unidentified_firmware_is_refused_before_any_write(lab):
    lab.set(firmware_info=["x", "y", "z", "V2.4.9"])
    for command in ("preflight", "apply"):
        done = lab.install(command, "--port", lab.port)
        assert done.returncode == 1 and "not the supported version" in done.stderr and "never flashes" in done.stderr, done.stderr
    lab.set(faults={"not_micropython": True})
    done = lab.install("apply", "--port", lab.port)
    assert done.returncode == 1 and "did not answer as MicroPython" in done.stderr and "Firmware: unknown" in done.stderr, done.stderr
    assert lab.writes() == []


# ---- installation ---------------------------------------------------------------------------------------------------------------
@lab_case(scenario="bare")
def a_first_installation_creates_every_file_and_reports_files_only_not_ready(lab):
    before = lab.files()
    done = lab.install("apply", "--port", lab.port, "--restart")
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "FILES INSTALLED AND VERIFIED: 15 file(s) written" in out, out
    assert_installed(lab, before)
    assert json.loads((lab.flash / "device_config.json").read_text()) == {"v": 1, "server_host": "192.0.2.10", "server_port": 50001}
    rec = lab.record()
    assert rec["status"] == "complete" and rec["assistant_ready"] is False and rec["installation_state_before"].startswith("no assistant")
    assert {f["action"] for f in rec["files"]} == {"create"} and len(rec["files"]) == 15
    assert [f["path"] for f in rec["files"]] == [f["path"] for f in MANIFEST["files"]]       # activation order: manifest order, main.py last
    assert all(f["staged_verified"] and f["activated"] and f["final_verified"] for f in rec["files"])
    assert rec["device_config_sha256"] == sha(expected_device_config()) and rec["install_manifest"]["path"] == "stick/install_manifest.json"
    folder = lab.records()[-1].parent
    for path in [folder, *folder.rglob("*")]:
        assert oct(path.stat().st_mode & 0o777) == ("0o700" if path.is_dir() else "0o600"), path
    assert not (lab.flash / ".stick-restore").exists() and lab.state().get("resets") == 1
    assert "boot option is not 0" in out and "boot option 1" in out and "network name no" in out
    not_ready(out)
    assert_no_leaks(lab, out)


@lab_case
def replacing_vendor_main_needs_a_complete_backup_of_this_stick(lab):
    before = lab.files()
    done = lab.install("apply", "--port", lab.port)
    assert done.returncode == 1 and "Take a complete backup first" in done.stderr and "/flash/main.py" in done.stderr, done.stderr
    assert lab.writes() == [] and lab.record()["stage"] == "compare" and lab.record()["status"] == "failed"
    folder = lab.backup()
    done = lab.install("apply", "--port", lab.port, "--backup", str(folder))
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "Backup checked" in out, out
    assert_installed(lab, before)
    actions = {f["path"]: f["action"] for f in lab.record()["files"]}
    assert actions.pop("/flash/main.py") == "replace" and set(actions.values()) == {"create"}
    assert lab.record()["backup"]["folder"] == str(folder.resolve())



@lab_case(scenario="previous")
def a_stale_or_foreign_backup_is_refused_before_any_write(lab):
    folder = lab.backup()
    with open(lab.flash / "robot.py", "a") as handle:                     # the Stick changed after the backup
        handle.write("# changed later\n")
    done = lab.install("apply", "--port", lab.port, "--backup", str(folder))
    assert done.returncode == 1 and "does not hold the version now on the Stick" in done.stderr and "/flash/robot.py" in done.stderr, done.stderr
    lab.set(unique_id="0f0e0d0c0b0a")
    done = lab.install("apply", "--port", lab.port, "--backup", str(folder))
    assert done.returncode == 1 and "not this Stick" in done.stderr, done.stderr
    damaged = Path(lab.tmp.name) / "damaged"
    shutil.copytree(folder, damaged)
    (damaged / "manifest.json").write_text("{")
    done = lab.install("apply", "--port", lab.port, "--backup", str(damaged))
    assert done.returncode == 1 and "cannot be used" in done.stderr
    assert lab.writes() == []


@lab_case(scenario="current")
def a_current_installation_and_a_repeated_run_write_nothing(lab):
    done = lab.install("preflight", "--port", lab.port)
    assert "INSTALLATION STATE: already installed" in done.stdout, done.stdout
    for _ in range(2):
        done = lab.install("apply", "--port", lab.port)
        assert done.returncode == 0 and "nothing to write" in done.stdout, done.stdout + done.stderr
    assert lab.writes() == []


@lab_case(scenario="bare")
def low_space_is_refused_and_nothing_is_deleted_to_make_room(lab):
    before = lab.files()
    lab.set(flash_free=300 * 1024)                                        # less than the ~473 KB a first installation stages
    done = lab.install("preflight", "--port", lab.port)
    assert "NOT ENOUGH SPACE" in done.stdout, done.stdout
    done = lab.install("apply", "--port", lab.port)
    assert done.returncode == 1 and "KB free" in done.stderr and "Nothing was written" in done.stderr, done.stderr
    assert lab.writes() == [] and lab.files() == before and lab.record()["stage"] == "space"


@lab_case(scenario="bare")
def conflicts_are_reported_and_nothing_is_written(lab):
    (lab.flash / "sounds").write_text("a FILE where the sounds folder belongs\n")
    (lab.flash / "mic.py").mkdir()
    done = lab.install("preflight", "--port", lab.port)
    assert "CONFLICTS" in done.stdout and "a file on the Stick blocks its folder" in done.stdout and "a folder has this name" in done.stdout
    done = lab.install("apply", "--port", lab.port)
    assert done.returncode == 1 and "CONFLICTS" in done.stdout and lab.writes() == []
    assert (lab.flash / "sounds").read_text().startswith("a FILE") and (lab.flash / "mic.py").is_dir()


# ---- interruption and resumption ------------------------------------------------------------------------------------------------
@lab_case(scenario="previous")
def an_interrupted_transfer_changes_no_app_file_and_a_rerun_completes(lab):
    before = lab.files()
    folder = lab.backup()
    lab.set(faults={"cp_to_fail": ["/flash/voice_send.py"]})
    done = lab.install("apply", "--port", lab.port, "--backup", str(folder))
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "stage 'staging'" in out and "FILES INSTALLED" not in out and "no app file was changed" in out, out
    assert lab.files() == before
    lab.set(faults={})
    done = lab.install("apply", "--port", lab.port, "--backup", str(folder))
    assert done.returncode == 0 and "FILES INSTALLED AND VERIFIED" in done.stdout, done.stdout + done.stderr
    assert_installed(lab, before)
    earlier = lab.record()["earlier_unfinished_installations"]
    assert len(earlier) == 1 and earlier[0]["status"] == "failed" and earlier[0]["stage"] == "staging"
    assert "/flash/server_config.py" in earlier[0]["created"] and "/flash/voice_send.py" in earlier[0]["replaced"]


@lab_case(scenario="previous")
def an_interrupted_activation_refuses_a_restart_and_the_rerun_accounts_for_created_files(lab):
    before = lab.files()
    folder = lab.backup()
    lab.set(faults={"activation_interrupt_after_renames": 5})
    done = lab.install("apply", "--port", lab.port, "--backup", str(folder), "--restart")
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "stage 'activating'" in out and "Do NOT restart" in out and "NOT restarted" in out, out
    assert lab.state().get("resets", 0) == 0 and "FILES INSTALLED" not in out
    first = lab.record()
    assert first["safe_to_restart_app"] is False and any(f["activated"] for f in first["files"]) and not all(f["activated"] for f in first["files"])
    lab.set(faults={})
    done = lab.install("apply", "--port", lab.port, "--backup", str(folder), "--restart")
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "FILES INSTALLED AND VERIFIED" in out and lab.state()["resets"] == 1, out
    after = lab.files()
    for path, data in install_set().items():
        assert after[path] == data, path
    second = lab.record()
    [earlier] = second["earlier_unfinished_installations"]
    created_first = {f["path"] for f in first["files"] if f["action"] == "create"}
    assert set(earlier["created"]) == created_first and earlier["stage"] == "activating"
    assert second["earlier_previous_versions_kept"], "the first run's previous versions must be reported, not deleted"


@lab_case(scenario="bare")
def a_run_killed_during_activation_is_never_complete_and_a_rerun_finishes(lab):
    lab.set(faults={"hang_on_exec_marker": "STICKACT"})
    proc = subprocess.Popen([sys.executable, str(TOOL), "apply", "--port", lab.port], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=lab.env(), stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 120
        while time.time() < deadline and not (lab.records() and lab.record()["stage"] == "activating"):
            time.sleep(0.2)
        time.sleep(0.5)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    rec = lab.record()
    assert rec["status"] == "in-progress" and rec["safe_to_restart_app"] is False
    lab.set(faults={})
    done = lab.install("apply", "--port", lab.port)
    assert done.returncode == 0 and "FILES INSTALLED AND VERIFIED" in done.stdout, done.stdout + done.stderr


@lab_case(scenario="bare")
def a_file_that_fails_final_verification_is_never_reported_installed(lab):
    lab.set(faults={"corrupt_after_activation": ["/flash/mic.py"]})
    done = lab.install("apply", "--port", lab.port, "--restart")
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "stage 'verify-final'" in out and "FILES INSTALLED" not in out and lab.state().get("resets", 0) == 0, out
    assert lab.record()["status"] == "failed"


@lab_case(scenario="bare")
def another_device_than_expected_is_refused_before_any_write(lab):
    for command in ("preflight", "apply"):
        done = lab.install(command, "--port", lab.port, "--expect-device", "000000000000")
        assert done.returncode == 1 and "not 000000000000" in done.stderr, done.stderr
    assert lab.writes() == []


@lab_case(scenario="bare")
def expected_changes_guard_the_write_set(lab):
    done = lab.install("apply", "--port", lab.port, "--expect-changes", "/flash/mic.py")
    assert done.returncode == 1 and "not exactly the expected ones" in done.stderr and lab.writes() == []


# ---- the upgrade of an existing v0.11.1 Stick, and its prepared recovery --------------------------------------------------------
RESTORE = ROOT / "tools" / "stick_restore.py"
RUNTIME = {"/flash/device_cache.json": b'{"note": "rewritten by the app after the upgrade"}', "/flash/reply.wav": b"RIFF-runtime-stand-in"}


def device_id():
    return hashlib.sha256(bytes.fromhex("a1b2c3d4e5f6")).hexdigest()[:12]


def upgrade(lab, folder, **env):
    args = ["apply", "--port", lab.port, "--backup", str(folder), "--expect-device", device_id()]
    for path in REPLACED + CREATED:
        args += ["--expect-changes", path]
    return lab.install(*args, **env)


def recover(lab, folder, record_dir, restore_changes):
    """The prepared recovery: restore ONLY the three replaced files from the backup, then remove ONLY the files the record lists as
    created (each only if unchanged). Returns the two runs' outputs."""
    args = ["apply", str(folder), "--port", lab.port] + sum((["--only", p] for p in REPLACED), [])
    args += sum((["--expect-changes", p] for p in restore_changes), [])
    restored = lab.run(*args, tool=RESTORE)
    removed = lab.install("remove-created", str(record_dir), "--port", lab.port, *sum((["--expect-remove", p] for p in CREATED), []))
    return restored, removed


def set_files(lab, files):
    for path, data in files.items():
        (lab.flash / path[7:]).write_bytes(data)


@lab_case(scenario="previous")
def upgrading_an_existing_stick_writes_exactly_five_files_and_the_prepared_recovery_returns_it(lab):
    before = lab.files()
    assert not set(CREATED) & set(before) and all(p in before for p in REPLACED)
    folder = lab.backup()
    done = upgrade(lab, folder)
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "FILES INSTALLED AND VERIFIED" in out, out[-2500:]
    assert_installed(lab, before)
    assert_no_leaks(lab, out)
    not_ready(out)
    rec_dir = lab.records()[-1].parent
    rec = lab.record()
    assert sorted(f["path"] for f in rec["files"] if f["action"] == "replace") == sorted(REPLACED)
    assert sorted(f["path"] for f in rec["files"] if f["action"] == "create") == sorted(CREATED)
    for unchanged in ("/flash/mic.py", "/flash/statick_shapes.py", "/flash/robot.py", "/flash/sounds/shake1.wav"):
        assert lab.files()[unchanged] == before[unchanged] == install_set()[unchanged], unchanged
    written = len(lab.writes())
    again = lab.install("preflight", "--port", lab.port)
    assert "already installed" in again.stdout and len(lab.writes()) == written
    set_files(lab, RUNTIME)                                                      # the app ran after the upgrade
    lab.log.write_text("")
    restored, removed = recover(lab, folder, rec_dir, REPLACED)
    assert restored.returncode == 0 and "RESTORE COMPLETE" in restored.stdout, restored.stdout + restored.stderr
    assert removed.returncode == 0 and "REMOVED AND VERIFIED ABSENT" in removed.stdout, removed.stdout + removed.stderr
    after = lab.files()
    assert set(after) == set(before) | set(RUNTIME), sorted(set(after) ^ set(before))
    for path, data in before.items():
        assert after[path] == (RUNTIME.get(path, data)), path                   # pre-install state; the app's own files left as it wrote them
    assert not (lab.flash / ".stick-restore").exists()
    assert lab.writes(), "the recovery wrote nothing?"
    for call in lab.writes():
        assert not any(name in json.dumps(call) for name in ("device_cache", "reply.wav", "voice_test", "boot.py")), call
    assert_no_leaks(lab, restored.stdout, removed.stdout)
    assert lab.state().get("resets", 0) == 0
    rec = json.loads((rec_dir / "record.json").read_text())
    assert sorted(rec["recovery"][-1]["removed"]) == sorted(CREATED) and rec["recovery"][-1]["still_present"] == []


@lab_case(scenario="previous")
def recovery_after_an_activation_interrupted_with_a_file_missing_returns_the_pre_install_files(lab):
    before = lab.files()
    folder = lab.backup()
    lab.set(faults={"activation_interrupt_after_renames": 3})       # both configs created, voice_send.py moved aside: the app could not start
    done = upgrade(lab, folder)
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "Do NOT restart" in out, out[-2000:]
    mid = lab.files()
    assert "/flash/voice_send.py" not in mid and all(p in mid for p in CREATED)
    lab.set(faults={})
    rec_dir = lab.records()[-1].parent
    compare = lab.run("compare", str(folder), "--port", lab.port, *sum((["--only", p] for p in REPLACED), []), tool=RESTORE)
    assert "would be CREATED (missing on the Stick): 1" in compare.stdout and "/flash/voice_send.py" in compare.stdout, compare.stdout
    restored, removed = recover(lab, folder, rec_dir, ["/flash/voice_send.py"])
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert lab.files() == before
    kept = [p for p in (lab.flash / ".stick-restore").rglob("*") if p.is_file()]
    assert kept and all("/prev/" in str(p) for p in kept), kept          # the interrupted run's previous versions are kept, never deleted
    assert lab.state().get("resets", 0) == 0


@lab_case(scenario="previous")
def remove_created_needs_the_exact_set_and_removes_nothing_it_cannot_account_for(lab):
    folder = lab.backup()
    assert upgrade(lab, folder).returncode == 0
    rec_dir = lab.records()[-1].parent
    lab.log.write_text("")
    partial = lab.install("remove-created", str(rec_dir), "--port", lab.port, "--expect-remove", CREATED[0])
    assert partial.returncode == 2 and "exactly the files this installation created" in partial.stderr and lab.calls() == [], partial.stderr
    extra = lab.install("remove-created", str(rec_dir), "--port", lab.port, *sum((["--expect-remove", p] for p in CREATED + ["/flash/boot.py"]), []))
    assert extra.returncode == 2 and lab.calls() == []
    (lab.flash / "server_config.py").write_bytes(b"# edited on the Stick after the installation\n")
    changed = lab.install("remove-created", str(rec_dir), "--port", lab.port, *sum((["--expect-remove", p] for p in CREATED), []))
    assert changed.returncode == 1 and "NOTHING was removed" in changed.stderr + changed.stdout, changed.stdout + changed.stderr
    assert (lab.flash / "server_config.py").exists() and (lab.flash / "device_config.json").exists() and lab.writes() == []
    bogus = lab.data / "not-an-install"
    bogus.mkdir()
    (bogus / "record.json").write_text(json.dumps({"kind": "restore", "files": []}))
    refused = lab.install("remove-created", str(bogus), "--port", lab.port, "--expect-remove", CREATED[0])
    assert refused.returncode == 2 and "not an installation record" in refused.stderr


@lab_case(scenario="previous")
def restore_only_refuses_files_the_backup_does_not_hold_and_leaves_other_files_alone(lab):
    before_notify = (lab.flash / "notify.py").read_bytes()
    folder = lab.backup()
    lab.log.write_text("")
    done = lab.run("apply", str(folder), "--port", lab.port, "--only", "/flash/server_config.py", tool=RESTORE)
    assert done.returncode == 2 and "does not hold" in done.stderr and lab.calls() == [], done.stderr
    set_files(lab, {"/flash/device_cache.json": RUNTIME["/flash/device_cache.json"], "/flash/notify.py": b"# changed\n"})
    done = lab.run("apply", str(folder), "--port", lab.port, "--only", "/flash/notify.py", "--expect-changes", "/flash/notify.py", tool=RESTORE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (lab.flash / "device_cache.json").read_bytes() == RUNTIME["/flash/device_cache.json"]
    assert (lab.flash / "notify.py").read_bytes() == before_notify


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
            print("ERROR", fn.__name__, repr(error)[:700])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
