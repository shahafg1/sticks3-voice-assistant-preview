"""Tests for tools/stick_backup.py (Stick identification and private file backup) against a SIMULATED Stick.
Run: python3 tests/test_stick_backup.py

Nothing here contacts a real Stick: the "device nodes" are files in a temporary folder (STICK_DEV_DIR), the Stick tool is
tests/fixtures/fake_stick_device.py behind a neutral name, and the isolation guard is active in every process. The fake runs the
tool's device-side snippets in CPython against a folder standing in for /flash; that checks their logic and the tool's handling,
not MicroPython or real hardware."""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "stick_backup.py"
FAKE = ROOT / "tests" / "fixtures" / "fake_stick_device.py"
SSID, PASSWORD = "wifi-name-SENTINEL-71", "wifi-pass-SENTINEL-72"
KEY = "device-key-SENTINEL-73"
CONTENT = "settings-SENTINEL-74"
SECRETS = (SSID, PASSWORD, KEY)

spec = importlib.util.spec_from_file_location("stick_backup_under_test", TOOL)
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)
PREFIX = sb.stick_config.USB_PREFIX                                   # the name macOS gives these boards (from the code, not a literal here)


def kit_files():
    text = (ROOT / "tools" / "deploy_stick.sh").read_text()
    return text.split("\nFILES=(")[1].split(")")[0].split()


class Lab:
    """A temporary 'Mac' (data folder, device-node folder, the simulated Stick tool) and one simulated Stick."""

    def __init__(self, faults=None, app=True, **state):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.dev = t / "fakedev"
        self.dev.mkdir()
        self.port = str(self.dev / (PREFIX + "SIM1"))      # a FILE named like a Stick port, in a temporary folder
        Path(self.port).touch()
        flash = t / "flash"
        (flash / "apps").mkdir(parents=True)
        (flash / "sounds").mkdir()
        if app:
            for name in kit_files():
                shutil.copy2(ROOT / "stick" / name, flash / name)
        (flash / "device_key.txt").write_text(KEY)
        (flash / "device_cache.json").write_text(json.dumps({"note": CONTENT}))
        (flash / "boot.py").write_text("# vendor boot file stand-in\n")
        (flash / "apps" / "demo.py").write_text("print('demo')\n")
        (flash / "sounds" / "shake1.wav").write_bytes(bytes(range(256)) * 40)
        self.flash, self.data = flash, t / "data"
        self.bin = t / "bin"
        self.bin.mkdir()
        self.program = self.bin / "fake-stick-device"
        self.program.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        self.program.chmod(0o755)
        self.config = t / "host.conf"
        self.config.write_text("[paths]\n")
        self.state_path, self.log = t / "device-state.json", t / "device-calls.log"
        base = {"flash_dir": str(flash), "unique_id": "a1b2c3d4e5f6", "other_unique_id": "0f0e0d0c0b0a",
                "uname": {"machine": "M5STACK StickS3 with ESP32-S3-PICO-1", "release": "1.27.0", "version": "v1.27.0-dirty on 2026-06-26"},
                "firmware_info": ["x", "y", "z", "V2.4.8"], "nvs": {"boot_option": 0, "ssid0": SSID, "pswd0": PASSWORD},
                "faults": faults or {}}
        base.update(state)
        self.state_path.write_text(json.dumps(base))
        self.log.write_text("")

    def env(self, **more):
        env = {k: v for k, v in os.environ.items() if not k.startswith("STICK_") and k != "MPREMOTE"}
        env.update({"STICK_CONFIG": str(self.config), "MPREMOTE": str(self.program), "STICK_DATA_DIR": str(self.data),
                    "STICK_DEV_DIR": str(self.dev), "FAKE_DEVICE_STATE": str(self.state_path), "FAKE_DEVICE_LOG": str(self.log),
                    "PYTHONPATH": isolation_guard.child_pythonpath()})
        env.update(more)
        return env

    def run(self, *args, tool=TOOL, timeout=180, **env):
        return subprocess.run([sys.executable, str(tool), *args], capture_output=True, text=True, env=self.env(**env),
                              stdin=subprocess.DEVNULL, timeout=timeout)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def copies(self):
        return [c[1] for c in self.calls() if c and c[0] == "COPY"]

    def state(self):
        return json.loads(self.state_path.read_text())

    def backups(self):
        root = self.data / "stick-backups"
        return sorted(p for p in root.glob("stick-*/*") if p.is_dir()) if root.exists() else []

    def manifest(self, folder):
        return json.loads((folder / "manifest.json").read_text())

    def close(self):
        self.tmp.cleanup()


def all_bytes(folder):
    return b"".join(p.read_bytes() for p in Path(folder).rglob("*") if p.is_file())


def no_secrets(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, ("secret value leaked", secret)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


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


# ---- ports ------------------------------------------------------------------------------------------------------------------
@lab_case
def a_port_is_never_chosen_automatically_and_nothing_is_contacted_without_one(lab):
    done = lab.run("identify")
    assert done.returncode == 2 and lab.port in done.stderr and "--port" in done.stderr and "never picks" in done.stderr, done.stderr
    second = lab.dev / (PREFIX + "SIM2")
    second.touch()
    done = lab.run("backup")
    assert done.returncode == 2 and lab.port in done.stderr and str(second) in done.stderr, done.stderr       # ambiguous: both listed
    done = lab.run("identify", "--port", str(lab.dev / (PREFIX + "GONE")))
    assert done.returncode == 2 and "not connected" in done.stderr
    done = lab.run("identify", "--port", str(lab.dev / "tty.something"))
    assert done.returncode == 2 and "does not look like" in done.stderr
    ports = lab.run("ports")
    assert ports.returncode == 0 and lab.port in ports.stdout and "nothing was opened" in ports.stdout
    assert lab.calls() == [], "the Stick was contacted"


# ---- identification ------------------------------------------------------------------------------------------------------------
@lab_case
def identify_reports_the_supported_firmware_and_presence_only(lab):
    done = lab.run("identify", "--port", lab.port)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    for expected in ("M5STACK StickS3", "1.27.0", "2026-06-26", "V2.4.8", "boot option        0", "network name: yes, password: yes",
                     "device key         present", "matches this kit", "SUPPORTED (the firmware", "stops the running assistant",
                     "MicroPython REPL", "not reset by this tool", "stopped until the Stick restarts"):
        assert expected in out, (expected, out)
    no_secrets(out, lab.log.read_text())
    assert lab.copies() == [] and lab.state().get("resets", 0) == 0
    assert "KEY-OPENED-ON-DEVICE" not in lab.log.read_text()
    data = json.loads(lab.run("identify", "--port", lab.port, "--json").stdout)
    assert data["wifi_ssid_saved"] is True and data["support"] == "supported" and data["device_id"] == hashlib.sha256(bytes.fromhex("a1b2c3d4e5f6")).hexdigest()[:12]


@lab_case(faults={"firmware_info_error": True}, uname={"machine": "M5STACK StickS3 with ESP32-S3-PICO-1"})
def values_that_cannot_be_read_are_reported_as_unknown(lab):
    done = lab.run("identify", "--port", lab.port, "--json")
    data = json.loads(done.stdout)
    assert done.returncode == 0 and data["uiflow"] == "unknown" and data["micropython"] == "unknown" and data["support"] == "unknown", data
    assert data["model"].startswith("M5STACK") and data["support_reasons"]


@lab_case(nvs={"boot_option": 1}, faults={"nvs_errors": {"pswd0": "error"}})
def wifi_presence_is_yes_no_or_unknown_never_a_value(lab):
    data = json.loads(lab.run("identify", "--port", lab.port, "--json").stdout)
    assert data["wifi_ssid_saved"] is False and data["wifi_password_saved"] == "unknown" and data["boot_option"] == 1, data


@lab_case(nvs={"ssid0": SSID, "pswd0": PASSWORD})
def a_missing_boot_option_is_reported_as_not_set(lab):
    data = json.loads(lab.run("identify", "--port", lab.port, "--json").stdout)
    assert data["boot_option"] == "not set", data


@lab_case(app=False)
def a_stick_without_the_assistant_is_not_installed(lab):
    data = json.loads(lab.run("identify", "--port", lab.port, "--json").stdout)
    assert data["app"]["status"] == "not installed", data["app"]


@lab_case(faults={"not_micropython": True})
def a_stick_that_does_not_answer_as_micropython_is_unknown_and_never_backed_up(lab):
    done = lab.run("identify", "--port", lab.port)
    assert done.returncode == 1 and "did not answer as MicroPython" in done.stderr and "unknown" in done.stderr
    done = lab.run("backup", "--port", lab.port)
    assert done.returncode == 1 and lab.backups() == [] and lab.copies() == []


@lab_case(firmware_info=["x", "y", "z", "V2.5.0"])
def unsupported_firmware_is_named_and_needs_an_explicit_choice_to_back_up(lab):
    done = lab.run("identify", "--port", lab.port)
    assert done.returncode == 0 and "NOT SUPPORTED" in done.stdout and "uiflow V2.5.0 (supported: V2.4.8)" in done.stdout, done.stdout
    done = lab.run("backup", "--port", lab.port)
    assert done.returncode == 1 and "--allow-unsupported" in done.stderr and lab.backups() == [] and lab.copies() == []
    done = lab.run("backup", "--port", lab.port, "--allow-unsupported")
    assert done.returncode == 0, done.stdout + done.stderr
    assert lab.manifest(lab.backups()[0])["device"]["support"] == "unsupported"


# ---- backup ------------------------------------------------------------------------------------------------------------------
@lab_case
def a_complete_backup_is_private_verified_and_excludes_the_key(lab):
    done = lab.run("backup", "--port", lab.port)
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "Backup COMPLETE" in out, out
    [folder] = lab.backups()
    assert folder.parent.name == "stick-" + hashlib.sha256(bytes.fromhex("a1b2c3d4e5f6")).hexdigest()[:12]
    for path in [folder.parent.parent, folder.parent, folder, *[p for p in folder.rglob("*") if p.is_dir()]]:
        assert oct(path.stat().st_mode & 0o777) == "0o700", path
    for path in [p for p in folder.rglob("*") if p.is_file()]:
        assert oct(path.stat().st_mode & 0o777) == "0o600", path
    m = lab.manifest(folder)
    assert m["status"] == "complete" and m["failures"] == [] and m["connection"] == sb.IMPACT and m["limits"] == sb.LIMITS
    expected = sorted("/" + str(p.relative_to(lab.flash.parent)).replace("flash", "flash", 1) for p in lab.flash.rglob("*") if p.is_file())
    assert sorted(r["path"] for r in m["files"]) == [p for p in expected if p != "/flash/device_key.txt"]
    for record in m["files"]:
        local = folder / "files" / record["path"][len("/flash/"):]
        data = local.read_bytes()
        assert record["verified"] and record["sha256"] == hashlib.sha256(data).hexdigest() == record["device_sha256"] and record["size"] == len(data)
    assert m["excluded"] == [{"path": "/flash/device_key.txt", "reason": sb.EXCLUDED["/flash/device_key.txt"]}]      # no size, no hash
    assert not (folder / "files" / "device_key.txt").exists() and "/flash/device_key.txt" not in lab.copies()
    assert "KEY-OPENED-ON-DEVICE" not in lab.log.read_text()
    blob = all_bytes(folder).decode("latin-1")
    no_secrets(blob, out, lab.log.read_text())
    assert CONTENT in blob and CONTENT not in out                    # the settings cache was copied, and never printed
    assert m["device"]["wifi_ssid_saved"] is True and lab.state().get("resets", 0) == 0


@lab_case
def a_second_backup_never_overwrites_the_first(lab):
    assert lab.run("backup", "--port", lab.port).returncode == 0
    [first] = lab.backups()
    before = {str(p): p.read_bytes() for p in first.rglob("*") if p.is_file()}
    time.sleep(1.1)
    assert lab.run("backup", "--port", lab.port).returncode == 0
    folders = lab.backups()
    assert len(folders) == 2 and folders[0] != folders[1]
    assert {str(p): p.read_bytes() for p in first.rglob("*") if p.is_file()} == before


@lab_case
def a_different_device_than_expected_is_refused_before_copying(lab):
    done = lab.run("backup", "--port", lab.port, "--expect-device", "000000000000")
    assert done.returncode == 1 and "not 000000000000" in done.stderr and lab.backups() == [] and lab.copies() == []


@lab_case(faults={"swap_id_after_calls": 3})
def a_device_that_changes_during_the_backup_makes_it_incomplete(lab):
    done = lab.run("backup", "--port", lab.port)
    m = lab.manifest(lab.backups()[0])
    assert done.returncode == 1 and m["status"] == "incomplete" and any("different device" in f["reason"] for f in m["failures"]), m["failures"]


@lab_case(faults={"disconnect_after_calls": 5})
def a_disconnect_marks_the_backup_incomplete_and_lists_what_was_not_attempted(lab):
    done = lab.run("backup", "--port", lab.port)
    m = lab.manifest(lab.backups()[0])
    assert done.returncode == 1 and "Backup INCOMPLETE" in done.stdout and m["status"] == "incomplete"
    assert any("disconnected" in f["reason"] for f in m["failures"]) and any(f["reason"] == "copy failed" for f in m["failures"])
    assert len(m["files"]) == 2                                       # calls 1-3: identify, app hashes, list; 4-5: two copies
    assert not Path(lab.port).exists()


@lab_case(faults={"truncate_files": ["/flash/robot.py"]})
def a_partial_read_is_detected_by_size_and_hash(lab):
    done = lab.run("backup", "--port", lab.port)
    m = lab.manifest(lab.backups()[0])
    assert done.returncode == 1 and m["status"] == "incomplete"
    bad = [r for r in m["files"] if r["path"] == "/flash/robot.py"][0]
    assert not bad["verified"] and any(f["path"] == "/flash/robot.py" and "partial or damaged" in f["reason"] for f in m["failures"])
    assert all(r["verified"] for r in m["files"] if r["path"] != "/flash/robot.py")


@lab_case(faults={"cp_fail": ["/flash/apps/demo.py"]})
def one_failed_copy_is_recorded_and_the_rest_still_verified(lab):
    done = lab.run("backup", "--port", lab.port)
    m = lab.manifest(lab.backups()[0])
    assert done.returncode == 1 and m["status"] == "incomplete" and m["failures"] == [{"path": "/flash/apps/demo.py", "reason": "copy failed"}]
    assert all(r["verified"] for r in m["files"])


@lab_case(faults={"truncate_exec_output_after_lines": 3})
def an_incomplete_file_list_stops_the_backup_before_copying(lab):
    done = lab.run("backup", "--port", lab.port)
    assert done.returncode == 1 and "not complete" in done.stderr and lab.backups() == [] and lab.copies() == []


@lab_case(faults={"inflate_sizes": 10 ** 15})
def insufficient_disk_space_is_refused_before_copying(lab):
    done = lab.run("backup", "--port", lab.port)
    assert done.returncode == 1 and "free space" in done.stderr and lab.backups() == [] and lab.copies() == []


@lab_case(faults={"hang_on_cp": 60})
def an_interrupted_backup_is_never_left_marked_complete(lab):
    proc = subprocess.Popen([sys.executable, str(TOOL), "backup", "--port", lab.port], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=lab.env(), stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not any((f / "manifest.json").exists() for f in lab.backups()):
            time.sleep(0.2)
        time.sleep(0.5)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)                          # as abrupt as a crash or a pulled power cord on the Mac side
        proc.wait()
    [folder] = lab.backups()
    assert lab.manifest(folder)["status"] == "in-progress"          # never "complete"; documented as incomplete


@lab_case
def restart_happens_only_when_asked(lab):
    assert lab.run("identify", "--port", lab.port).returncode == 0 and lab.state().get("resets", 0) == 0
    done = lab.run("identify", "--port", lab.port, "--restart")
    assert done.returncode == 0 and "asked to restart" in done.stdout and lab.state()["resets"] == 1


@case
def a_backup_folder_inside_the_project_that_git_would_track_is_refused():
    lab = Lab()
    try:
        project = Path(lab.tmp.name) / "kit copy"
        for rel in ("tools/stick_backup.py", "stick_config.py", "tools/deploy_stick.sh", *("stick/" + n for n in kit_files())):
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, project / rel)
        assert subprocess.run(["git", "init", "-q", str(project)], capture_output=True).returncode == 0
        env = {k: v for k, v in lab.env().items() if k != "STICK_DATA_DIR"}            # the default: <project>/data
        done = subprocess.run([sys.executable, str(project / "tools" / "stick_backup.py"), "backup", "--port", lab.port], capture_output=True,
                              text=True, env=env, stdin=subprocess.DEVNULL, timeout=120)
        assert done.returncode == 2 and "NOT ignored by git" in done.stderr and lab.calls() == [], done.stderr
        (project / ".gitignore").write_text("data/\n")
        done = subprocess.run([sys.executable, str(project / "tools" / "stick_backup.py"), "backup", "--port", lab.port], capture_output=True,
                              text=True, env=env, stdin=subprocess.DEVNULL, timeout=180)
        assert done.returncode == 0, done.stdout + done.stderr
        assert list((project / "data" / "stick-backups").glob("stick-*/*/manifest.json"))
    finally:
        lab.close()


# ---- units --------------------------------------------------------------------------------------------------------------------
@case
def classification_supports_only_the_validated_firmware():
    good = {"machine": "M5STACK StickS3 with ESP32-S3-PICO-1", "release": "1.27.0", "version": "v1.27.0-dirty on 2026-06-26", "uiflow": "V2.4.8"}
    assert sb.classify(good)[0] == "supported"
    for change in ({"release": "1.28.0"}, {"version": "v1.27.0 on 2026-07-01"}, {"uiflow": "V2.4.9"}, {"machine": "M5STACK Core2"}):
        assert sb.classify({**good, **change})[0] == "unsupported", change
    for missing in ("machine", "release", "version", "uiflow"):
        assert sb.classify({k: v for k, v in good.items() if k != missing})[0] == "unknown", missing
    assert sb.classify({**good, "uiflow": "garbage"})[0] == "unknown"


@case
def unsafe_names_never_leave_the_backup_folder():
    assert sb.safe_relative("/flash/a/b.py") == "a/b.py"
    for bad in ("/flash/../x", "/flash/a/../../x", "/flash/", "/other/x", "/flash/a//b", "/flash/a\\b", "/flash/./x"):
        assert sb.safe_relative(bad) is None, bad


@case
def the_device_snippets_print_only_markers():
    for snippet in (sb.IDENTIFY_SNIPPET, sb.LIST_SNIPPET, sb.HASH_SNIPPET):
        prints = [line.strip() for line in snippet.splitlines() if "print(" in line]
        assert prints and all(line.startswith('print("STICK') for line in prints), prints
    assert "get_str" in sb.IDENTIFY_SNIPPET and "bool(v)" in sb.IDENTIFY_SNIPPET and "v = None" in sb.IDENTIFY_SNIPPET


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:400])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:400])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
