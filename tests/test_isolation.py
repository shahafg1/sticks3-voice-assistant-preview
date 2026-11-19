"""Regression checks for test isolation (see tests/README.md). Run: python3 tests/test_isolation.py

These checks never touch production: the guard blocks before any operation, probes use sys.audit (no real I/O), and the
"production default" trap is exercised on a stand-in project directory created in a temporary location.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import isolation_guard as guard

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# This suite deliberately provokes violations to prove the guard works: keep those out of the runner's violation log
# (the log exists to catch violations by OTHER suites). The one test that checks logging sets the variable itself.
os.environ.pop("STICK_TEST_GUARD_LOG", None)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HOME = Path(os.path.expanduser("~"))
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def blocked(event, *args):
    """True if the guard stops this operation. sys.audit only runs the hooks: nothing is opened, connected or started."""
    try:
        sys.audit(event, *args)
    except guard.IsolationViolation:
        return True
    return False


# ---- static rules over every suite --------------------------------------------------------------------------

SUITES = sorted(p for p in HERE.glob("test_*.py") if p.name != "test_isolation.py")


@case
def every_suite_installs_the_guard_before_anything_else():
    assert SUITES
    for path in SUITES:
        first = next(line for line in path.read_text().splitlines() if line.startswith(("import ", "from ")))
        assert first.startswith("import isolation_guard"), f"{path.name} must import isolation_guard first, not: {first}"


@case
def suites_contain_no_production_literals():
    problems = []
    for path in SUITES:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#")[0]
            patching = ".replace(" in code                       # harness lines that REMOVE the production path/port from a copy of the server
            where = f"{path.name}:{number}: {line.strip()[:90]}"
            if re.search(r"/Users/(?!me/)", code):
                problems.append("hard-coded home path " + where)
            if re.search(r"mpremote|usbmodem|/dev/cu\.|/dev/tty|\bserial\b", code):
                problems.append("hardware access " + where)
            if re.search(r"\.claude\b", code) and "claude_log" not in code:
                problems.append("real Claude settings " + where)
            if not patching and re.search(r"10\.0\.0\.7|:800[01]\b|\b800[01]\)", code) and "8000 * " not in code and "t1=8000" not in code \
                    and "not in url" not in code:                # (test_hooks' own assertion that a URL is NOT a live port)
                problems.append("live endpoint " + where)
            if not patching and "afplay" not in code and re.search(r"Path\.home\(\)|expanduser|\$HOME|environ\[.HOME.\]", code):
                problems.append("home directory " + where)
    assert not problems, "\n" + "\n".join(problems)


@case
def harnesses_that_start_python_children_pass_the_guard_on():
    for path in SUITES:
        text = path.read_text()
        if "server.py" in text and "Popen(" in text:
            assert "child_pythonpath" in text, f"{path.name} builds its own PYTHONPATH without the guard"
            assert "STICK_DATA_DIR" in text and "STICK_SECRETS_DIR" in text, f"{path.name} must point the server at temporary data/secrets"


@case
def hardware_and_manual_checks_are_not_part_of_the_automated_suite():
    readme = (HERE / "README.md").read_text()
    assert "Hardware and manual checks do not live here" in readme
    runner = (HERE / "run_all.py").read_text()
    assert 'glob("test_*.py")' in runner and "tests" in runner
    for path in (ROOT / "tools").glob("*"):
        assert not path.name.startswith("test_"), f"{path} looks like a test but is a tool"


# ---- the guard itself -------------------------------------------------------------------------------------------

@case
def guard_blocks_production_paths_and_allows_temporary_ones():
    for path in (ROOT / "data" / "panel.db", ROOT / "secrets" / "device_key", ROOT / "recordings" / "reply.wav",
                 HOME / "sticks3-project" / "data" / "panel.db", HOME / "sticks3-project" / "secrets", HOME / ".claude" / "settings.json",
                 "/flash/device_key.txt", "/dev/cu.usbmodemTEST01"):
        assert blocked("open", str(path), "rb", 0), path
        assert blocked("os.mkdir", str(path), 0o700, -1), path
        assert blocked("os.listdir", str(path)), path
    assert blocked("sqlite3.connect", str(ROOT / "data" / "panel.db"))          # SQLite bypasses open(): it has its own event
    assert blocked("shutil.copyfile", str(ROOT / "README.md"), str(ROOT / "data" / "x"))    # destination counts too
    with tempfile.TemporaryDirectory() as tmp:
        assert not blocked("open", str(Path(tmp) / "panel.db"), "rb", 0)
        assert not blocked("sqlite3.connect", str(Path(tmp) / "panel.db"))
    assert not blocked("open", str(ROOT / "panel" / "index.html"), "rb", 0)       # reading the project's source is fine
    assert not blocked("open", str(ROOT / "datasheet.txt"), "rb", 0)              # only the directory, not a name prefix


@case
def guard_blocks_the_live_service_and_allows_local_test_servers():
    for address in ((isolation_guard._OWN_LAN or "192.0.2.1", 8000), ("127.0.0.1", 8000), ("127.0.0.1", 8001), ("localhost", 8001), ("0.0.0.0", 8000)):
        assert blocked("socket.connect", None, address), address
        assert blocked("socket.bind", None, address), address
    if guard._OWN_LAN:
        assert blocked("socket.connect", None, (guard._OWN_LAN, 8000))             # the live port is blocked on the LAN address too
        assert not blocked("socket.connect", None, (guard._OWN_LAN, 50321))         # but a test server there is fine
    for address in (("203.0.113.9", 443), ("example.com", 80), ("10.99.99.99", 5000)):
        assert blocked("socket.connect", None, address), address                    # no outside network, no other machine on the LAN
    for address in (("127.0.0.1", 0), ("127.0.0.1", 50123), ("localhost", 50124), ("192.0.2.1", 9)):
        assert not blocked("socket.connect", None, address), address


@case
def guard_blocks_the_real_claude_cli_the_launcher_and_the_stick_tools():
    assert blocked("subprocess.Popen", "/opt/homebrew/bin/claude", ["claude", "-p"], None, None)
    assert blocked("subprocess.Popen", "mpremote", ["mpremote", "connect"], None, None)
    assert blocked("subprocess.Popen", "/bin/zsh", ["zsh", "Start Voice Assistant.command"], None, None)
    assert blocked("subprocess.Popen", "python", ["python", "x.py", "/dev/cu.usbmodemTEST01"], None, None)
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "claude"
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
        assert not blocked("subprocess.Popen", str(stub), [str(stub), "-p"], None, None)      # a test stub is how the suites fake it


@case
def a_violation_cannot_be_swallowed_by_the_code_under_test_and_is_logged():
    log = Path(tempfile.mkdtemp()) / "guard.log"
    os.environ["STICK_TEST_GUARD_LOG"] = str(log)
    try:
        try:
            try:
                sys.audit("open", str(ROOT / "data" / "x"), "rb", 0)
            except Exception:                                                     # noqa: BLE001 - what sloppy production code does
                raise AssertionError("an ordinary `except Exception` swallowed the violation")
        except guard.IsolationViolation:
            pass
        assert "isolation violation" in log.read_text()
    finally:
        os.environ.pop("STICK_TEST_GUARD_LOG", None)
        shutil.rmtree(log.parent, ignore_errors=True)


def run_child(code, extra_env=None, pythonpath=()):
    env = {k: v for k, v in os.environ.items() if not k.startswith("STICK_") and k != "PYTHONPATH"}
    env["STICK_TEST_GUARD"] = "1"
    env["PYTHONPATH"] = guard.child_pythonpath(*pythonpath)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)


@case
def python_children_started_by_a_harness_are_guarded_too():
    probe = f"import sys; sys.audit('open', {str(ROOT / 'secrets' / 'device_key')!r}, 'rb', 0); print('NOT BLOCKED')"
    result = run_child(probe)                                                     # the env a harness builds from scratch
    assert result.returncode != 0 and "isolation violation" in result.stderr and "NOT BLOCKED" not in result.stdout, result
    assert run_child("print('fine')").stdout.strip() == "fine"                    # and an innocent child still runs


@case
def a_server_started_without_its_isolation_environment_is_trapped():
    """The incident: a test started the server without STICK_DATA_DIR and it migrated the real database. Rebuilt on a stand-in
    'production' directory in a temporary location: the real default path logic runs, the guard must stop it before anything is created."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        for name in ("device_link.py", "settings_store.py", "reminders.py"):
            shutil.copy(ROOT / name, project / name)
        extra = {"STICK_TEST_FORBIDDEN": os.pathsep.join([str(project / "data"), str(project / "secrets")])}
        result = run_child("import device_link; device_link.DeviceLink(lambda a, i: None); print('CREATED')", extra, [str(project)])
        assert result.returncode != 0 and "isolation violation" in result.stderr and "CREATED" not in result.stdout, result
        assert not (project / "data").exists() and not (project / "secrets").exists()
        ok = run_child("import device_link; device_link.DeviceLink(lambda a, i: None); print('CREATED')",
                       {**extra, "STICK_DATA_DIR": str(Path(tmp) / "t" / "data"), "STICK_SECRETS_DIR": str(Path(tmp) / "t" / "secrets")}, [str(project)])
        assert "CREATED" in ok.stdout, ok                                         # the same code is fine once isolated
        assert not (project / "data").exists()


@case
def the_settings_defaults_in_this_checkout_are_what_the_guard_protects():
    """If the production default for the panel database, secrets or recordings ever moves, the guard's forbidden list must move with it."""
    import stick_config
    sources = {n: (ROOT / n).read_text() for n in ("device_link.py", "audio_server.py")}
    assert '"data"' in sources["device_link.py"] and '"secrets"' in sources["device_link.py"]
    assert "CONFIG.recordings_dir" in sources["audio_server.py"]                       # the recordings folder now comes from the host configuration...
    assert stick_config.PATH_DEFAULTS["recordings_dir"] == "recordings" and stick_config.PATH_DEFAULTS["data_dir"] == "data" \
        and stick_config.PATH_DEFAULTS["secrets_dir"] == "secrets"                      # ...whose defaults are exactly the folders the guard forbids
    names = {os.path.basename(p) for p in guard.forbidden_prefixes()}
    assert {"data", "secrets", "recordings", ".claude"} <= names


@case
def guard_blocks_child_processes_aimed_at_the_live_service_and_scripts_started_without_explicit_test_ports():
    """Regression (ported from feature/mac-audio-output): a launcher test, with no ports given, fell back to the DEFAULT ports and
    probed the live panel. The LAN address is taken from the guard, so this file names no live address."""
    lan = sorted(guard.LIVE_HOSTS)[0]
    for target in ("http://127.0.0.1:8001/api/status", "localhost:8000", "http://%s:8000/status" % lan, "0.0.0.0:8001", "http://[::1]:8001/"):
        assert blocked("subprocess.Popen", "curl", ["curl", "-s", target], None, None), target
    assert not blocked("subprocess.Popen", "curl", ["curl", "-s", "http://127.0.0.1:50321/api/status"], None, None)
    assert not blocked("subprocess.Popen", "curl", ["curl", "-s", "http://127.0.0.1:18001/x"], None, None)       # a different port that merely starts alike
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "launcher-under-test.sh"
        script.write_text("#!/bin/zsh\nprobe \"$PANEL_PORT\"\n")                 # a script that knows about the ports, like a desktop launcher
        harmless = Path(tmp) / "tidy.sh"
        harmless.write_text("#!/bin/zsh\necho hello\n")
        ok = {"STICK_DEVICE_PORT": "50001", "STICK_PANEL_PORT": "50002"}
        saved = {k: os.environ.pop(k, None) for k in ok}
        try:
            assert blocked("subprocess.Popen", "/bin/zsh", ["zsh", str(script)], None, None)                  # inherits an environment with no ports
            assert blocked("subprocess.Popen", "/bin/zsh", ["zsh", str(script)], None, {"HOME": "/x"})
            assert blocked("subprocess.Popen", "/bin/bash", ["bash", str(script)], None, {"STICK_DEVICE_PORT": "50001"})   # only one of the two
            assert blocked("subprocess.Popen", "/bin/zsh", ["zsh", str(script)], None, {"STICK_DEVICE_PORT": "8000", "STICK_PANEL_PORT": "8001"})
            assert blocked("subprocess.Popen", "/bin/zsh", ["zsh", str(script)], None, {"STICK_DEVICE_PORT": "50001", "STICK_PANEL_PORT": "50001"})
            assert not blocked("subprocess.Popen", "/bin/zsh", ["zsh", str(script)], None, ok)                 # explicit, distinct, non-live ports
            assert not blocked("subprocess.Popen", "/bin/zsh", ["zsh", "-n", str(script)], None, {"HOME": "/x"})    # a syntax check runs nothing
            assert not blocked("subprocess.Popen", "/bin/zsh", ["zsh", str(harmless)], None, {"HOME": "/x"})    # a script that never mentions the ports is not the risk
            os.environ.update(ok)
            assert not blocked("subprocess.Popen", "/bin/zsh", ["zsh", str(script)], None, None)                # inherited explicit ports count
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:600])
        except Exception as error:                                                # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:400])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
