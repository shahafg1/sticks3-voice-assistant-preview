"""Tests for the Mac host setup (tools/setup_mac.py, setup-mac.sh).

REAL here: virtual environments are really created with `python -m venv` and packages really installed with `pip install --require-hashes` (from a tiny local
wheel, so no network), files are really downloaded over HTTP from a local test server (including Range resume, truncated and corrupted answers), real
processes hold and abandon the setup lock, and the shell entry point really runs.
SIMULATED here: the package index and Hugging Face (a local wheel and a local HTTP server stand in), the Python versions (the interpreter running this test
stands in for the tested versions), and every Mac-specific condition (Rosetta, missing tools). This is NOT a clean-Mac rehearsal and downloads nothing from the
internet: the full real installation is a manual procedure (docs/mac-setup.md, "Real installation rehearsal").

Everything runs in temporary folders (names with spaces and brackets), never touches the live project, its data or the Stick.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import base64
import contextlib
import hashlib
import http.server
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import stick_config as sc  # noqa: E402
import setup_mac as sm  # noqa: E402

CASES = []
PYVER = "%d.%d" % sys.version_info[:2]
ADDRESS = "192.0.2.10"


def case(fn):
    CASES.append(fn)
    return fn


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def build_wheel(folder, name="tinypkg", version="1.0"):
    dist = "%s-%s.dist-info" % (name, version)
    files = {"%s/__init__.py" % name: b"VALUE = 1\n",
             "%s/METADATA" % dist: ("Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, version)).encode(),
             "%s/WHEEL" % dist: b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = "".join("%s,sha256=%s,%d\n" % (n, b64(hashlib.sha256(d).digest()), len(d)) for n, d in files.items()) + "%s/RECORD,,\n" % dist
    files["%s/RECORD" % dist] = record.encode()
    path = Path(folder) / ("%s-%s-py3-none-any.whl" % (name, version))
    with zipfile.ZipFile(str(path), "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return path


def blob(seed, size):
    out, counter = b"", 0
    while len(out) < size:
        out += hashlib.sha256(("%s-%d" % (seed, counter)).encode()).digest()
        counter += 1
    return out[:size]


MODEL_FILES = {"tts": {"a.bin": blob("a", 200000), "b.bin": blob("b", 3000), "c.json": b'{"ok": true}\n'},
               "whisper": {"w.bin": blob("w", 50000), "config.json": b'{"n": 1}\n'}}
REVISION = "r" * 40


class ModelServer:
    """A local stand-in for huggingface.co: /<repo>/resolve/<revision>/<name>, with Range support and misbehaving modes."""

    def __init__(self):
        self.mode, self.dead_after, self.requests = "ok", 0, []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = self.path.strip("/").split("/")
                data = None
                for group in MODEL_FILES.values():
                    if parts[-1] in group:
                        data = group[parts[-1]]
                outer.requests.append((parts[-1], self.headers.get("Range")))
                if data is None or parts[-2] != REVISION:
                    self.send_error(404)
                    return
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng[6:].split("-")[0])
                body = data[start:]
                if outer.mode == "corrupt":
                    body = bytes([body[0] ^ 1]) + body[1:] if body else body
                if outer.mode == "short":
                    body = body[:-10]
                self.send_response(206 if rng else 200)
                self.send_header("Content-Length", str(len(body) if outer.mode != "dead" else len(body)))
                self.end_headers()
                if outer.mode == "dead":
                    allowed = max(0, outer.dead_after - start)
                    self.wfile.write(body[:allowed])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Lab:
    def __init__(self, python=PYVER, envs=("voice_env", "tools_env")):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()
        self.t = t
        self.root = t / "Setup Lab (copy) [one] v1"
        for rel in ("stick_config.py", "tools/setup_mac.py", "setup-mac.sh", "config/host.example.conf"):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(ROOT / rel), str(target))
        wheel = build_wheel(t)
        lock = "tinypkg @ %s \\\n    --hash=sha256:%s\n" % (wheel.as_uri(), hashlib.sha256(wheel.read_bytes()).hexdigest())
        (self.root / "requirements").mkdir()
        for name in ("server", "stick-tools"):
            (self.root / "requirements" / (name + ".lock")).write_text(lock)
        spec = {"schema": 1, "environments": [{"id": e, "title": "Test environment " + e, "python": python, "lock": "requirements/%s.lock" % ("server" if e == "voice_env" else "stick-tools"),
                                               "smoke": ["tinypkg"], "approx_bytes": 1000} for e in envs]}
        (self.root / "requirements" / "setup.json").write_text(json.dumps(spec))
        manifest = {}
        for key, folder in (("tts", "models_dir"), ("whisper", "whisper_dir")):
            manifest[key] = {"repo": "test/" + key, "revision": REVISION, "folder": folder,
                             "files": [{"name": n, "bytes": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in MODEL_FILES[key].items()]}
        (self.root / "requirements" / "models.json").write_text(json.dumps(manifest))
        self.server = ModelServer()
        self.overrides = {PYVER: sys.executable}
        (t / "home").mkdir()

    def setup(self, **kw):
        s = sm.Setup(root=self.root, python_overrides=self.overrides, models_base=self.server.base, **kw)
        s.retry_delay = 0
        return s

    def cli(self, *args, env=None, cwd=None):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("STICK_")}
        environment.update({"HOME": str(self.t / "home"), "PYTHONPATH": isolation_guard.child_pythonpath()})
        environment.update(env or {})
        (self.t / "home").mkdir(exist_ok=True)
        return subprocess.run([sys.executable, str(self.root / "tools" / "setup_mac.py"), *args, "--python", "%s=%s" % (PYVER, sys.executable), "--models-base", self.server.base],
                               capture_output=True, text=True, env=environment, cwd=str(cwd or self.t), timeout=300, stdin=subprocess.DEVNULL)

    def spawn_cli(self, *args):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("STICK_")}
        environment.update({"HOME": str(self.t / "home"), "PYTHONPATH": isolation_guard.child_pythonpath()})
        (self.t / "home").mkdir(exist_ok=True)
        return subprocess.Popen([sys.executable, str(self.root / "tools" / "setup_mac.py"), *args, "--python", "%s=%s" % (PYVER, sys.executable), "--models-base", self.server.base],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment, stdin=subprocess.DEVNULL)

    def env_dir(self, name):
        return self.root / sc.PATH_DEFAULTS[name]

    def close(self):
        self.server.close()
        self.tmp.cleanup()


def call(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*a, **k)
    return code, buf.getvalue()


def snapshot(*folders):
    out = {}
    for folder in folders:
        folder = Path(folder)
        if not folder.exists():
            continue
        for p in sorted([folder] + list(folder.rglob("*"))):
            try:
                st = p.lstat()
                out[str(p)] = (st.st_size, st.st_mtime_ns, p.is_symlink())
            except OSError:
                pass
    return out


def runs(env_dir, code="import tinypkg"):
    return subprocess.run([str(Path(env_dir) / "bin" / "python"), "-c", code], capture_output=True).returncode == 0


def with_lab(**kw):
    def wrap(fn):
        def run():
            lab = Lab(**kw)
            try:
                fn(lab)
            finally:
                lab.close()
        run.__name__ = fn.__name__
        return case(run)
    return wrap


def models_ok(lab):
    for key, folder in (("tts", lab.root / "tts-models"), ("whisper", lab.root / "whisper-model")):
        for name, data in MODEL_FILES[key].items():
            assert (folder / name).read_bytes() == data, name
    assert not list(lab.root.rglob("*.stick-setup.part"))


# ---- fresh and repeated setup -----------------------------------------------------------------------------------------------

@with_lab()
def a_fresh_setup_really_builds_the_environments_verifies_the_models_and_reports_that_the_assistant_is_not_ready_yet(lab):
    code, out = call(lab.setup().apply, ADDRESS)
    assert code == 0, out
    for name in ("voice_env", "tools_env"):
        d = lab.env_dir(name)
        assert (d / sm.OWNED).is_file() and not (d / sm.PARTIAL).exists() and runs(d), name        # a real venv, a real pip install, a real import
    models_ok(lab)
    assert "advertise_host = %s" % ADDRESS in (lab.root / "config" / "host.conf").read_text()
    assert "HOST PREPARATION: complete" in out and "THE ASSISTANT IS NOT READY YET" in out and "assistant is ready" not in out.lower()
    for heading in ("Claude account", "Network:", "The Stick:"):
        assert heading in out, heading                                                             # the three separate, still-open areas
    state = (lab.root / ".stick-setup" / "state.json").read_text()
    assert not any(word in state.lower() for word in ("password", "token", "secret", "api_key", "device_key"))
    assert not (lab.root / ".stick-setup" / "lock").exists()                                       # the lock is released
    assert stat_mode(lab.root / ".stick-setup") == 0o700


def stat_mode(path):
    return os.stat(str(path)).st_mode & 0o777


@with_lab()
def repeating_the_setup_changes_nothing_and_downloads_nothing(lab):
    assert call(lab.setup().apply, ADDRESS)[0] == 0
    before_requests = list(lab.server.requests)
    marks = {n: (lab.env_dir(n) / sm.OWNED).stat().st_mtime_ns for n in ("voice_env", "tools_env")}
    before = snapshot(lab.root / "tts-models", lab.root / "whisper-model", lab.root / "config")
    code, out = call(lab.setup().apply, None)                                                      # no address needed: the configuration exists
    assert code == 0 and "already complete" in out, out
    assert lab.server.requests == before_requests
    assert marks == {n: (lab.env_dir(n) / sm.OWNED).stat().st_mtime_ns for n in marks}
    assert snapshot(lab.root / "tts-models", lab.root / "whisper-model", lab.root / "config") == before


# ---- interruption and resume -----------------------------------------------------------------------------------------------

@with_lab()
def an_interrupted_environment_is_rebuilt_from_the_real_state_and_a_stale_completion_note_is_not_trusted(lab):
    s = lab.setup()
    original = sm.Setup.run_logged
    state = {"armed": True}

    def interrupt(self, command, log, what):
        if what == "installing the packages" and state["armed"]:
            raise KeyboardInterrupt()
        return original(self, command, log, what)

    sm.Setup.run_logged = interrupt
    try:
        try:
            call(s.apply, ADDRESS)
            raise AssertionError("the interruption did not happen")
        except KeyboardInterrupt:
            pass
    finally:
        sm.Setup.run_logged = original
    d = lab.env_dir("voice_env")
    assert (d / sm.PARTIAL).is_file() and not (d / sm.OWNED).exists()                              # left half-built, and marked as such
    assert not (lab.root / ".stick-setup" / "lock").exists()                                       # the interrupted run released its lock
    plan = lab.setup().build_plan()
    step = [x for x in plan["steps"] if x["id"] == "voice_env"][0]
    assert step["status"] == "todo" and "incomplete" in step["detail"]
    code, out = call(lab.setup().apply, None)
    assert code == 0 and runs(d) and (d / sm.OWNED).is_file(), out
    # a completion marker alone proves nothing: remove the package behind its back
    site = list((d / "lib").glob("python*/site-packages/tinypkg"))[0]
    shutil.rmtree(str(site))
    assert (d / sm.OWNED).is_file()
    step = [x for x in lab.setup().build_plan()["steps"] if x["id"] == "voice_env"][0]
    assert step["status"] == "todo" and "no longer load" in step["detail"], step
    code, out = call(lab.setup().apply, None)
    assert code == 0 and runs(d), out


@with_lab()
def an_interrupted_download_resumes_from_what_was_received_and_only_a_verified_file_is_activated(lab):
    lab.server.mode, lab.server.dead_after = "dead", 70000
    code, out = call(lab.setup().apply, ADDRESS)
    assert code == 1 and "FAILED" in out, out
    part = lab.root / "tts-models" / ".a.bin.stick-setup.part"
    assert part.is_file() and part.stat().st_size == 70000 and not (lab.root / "tts-models" / "a.bin").exists()
    lab.server.mode, lab.server.requests[:] = "ok", []
    code, out = call(lab.setup().apply, None)
    assert code == 0, out
    assert ("a.bin", "bytes=70000-") in lab.server.requests                                          # it continued, it did not start over
    models_ok(lab)


@with_lab()
def corrupted_or_truncated_downloads_are_discarded_and_never_activated(lab):
    for mode in ("corrupt", "short"):
        lab.server.mode = mode
        code, out = call(lab.setup().apply, ADDRESS)
        assert code == 1 and "does not match the pinned size/checksum" in out, (mode, out)
        folder = lab.root / "tts-models"
        assert not (folder / "a.bin").exists() and not list(folder.glob(".*part")), mode
        assert not any(k.startswith("file:") and "a.bin" in k for k in json.loads((lab.root / ".stick-setup" / "state.json").read_text())["resources"])
    lab.server.mode = "ok"
    assert call(lab.setup().apply, None)[0] == 0
    models_ok(lab)


# ---- prerequisites and refusals ---------------------------------------------------------------------------------------------

@with_lab(python="3.99")
def a_missing_python_stops_before_anything_is_created_and_the_plan_says_how_to_get_it(lab):
    s = lab.setup()
    plan_code, plan_out = call(lambda: (s.print_plan(s.build_plan()), 0)[1])
    assert "MISSING" in plan_out and "brew install python@3.99" in plan_out and "python.org/downloads/macos" in plan_out
    code, out = call(s.apply, ADDRESS)
    assert code == 2 and "Stopped before changing anything" in out, out
    assert not (lab.root / ".stick-setup").exists() and not lab.env_dir("voice_env").exists() and not (lab.root / "config" / "host.conf").exists()
    assert not (lab.root / "tts-models").exists() and not (lab.root / "whisper-model").exists()


@with_lab()
def missing_ffmpeg_and_claude_are_guided_not_installed_and_do_not_block_host_preparation(lab):
    (lab.root / "config").mkdir(exist_ok=True)
    (lab.root / "config" / "host.conf").write_text("[server]\nadvertise_host = %s\n[paths]\nffmpeg = /nonexistent/ffmpeg\nclaude = /nonexistent/claude\n" % ADDRESS)
    plan = lab.setup().build_plan()
    missing = {p["id"]: p for p in plan["prerequisites"] if p["status"] == "missing"}
    assert set(missing) == {"ffmpeg", "claude"}
    assert "brew install ffmpeg" in missing["ffmpeg"]["guide"] and "claude.ai/install.sh" in missing["claude"]["guide"] and "brew install --cask claude-code" in missing["claude"]["guide"]
    assert "Node" in missing["claude"]["guide"] and not missing["ffmpeg"]["blocks"] and not missing["claude"]["blocks"]
    code, out = call(lab.setup().apply, None)
    assert code == 0 and "HOST PREPARATION: complete" in out, out
    assert "System tools: ffmpeg, Claude Code CLI missing" in out and "THE ASSISTANT IS NOT READY YET" in out


@with_lab()
def setup_needs_an_explicit_address_and_refuses_unusable_ones_before_changing_anything(lab):
    for host, fragment in ((None, "choose the address"), ("127.0.0.1", "cannot be used"), ("not a host!", "cannot be used")):
        code, out = call(lab.setup().apply, host)
        assert code == 2 and "Stopped before changing anything" in out and fragment in out, (host, out)
        assert not (lab.root / ".stick-setup").exists() and not lab.env_dir("voice_env").exists() and not (lab.root / "config" / "host.conf").exists(), host
    assert "10.0.0." not in out


@with_lab()
def an_intel_or_rosetta_terminal_is_refused_before_changing_anything(lab):
    import platform
    original = platform.machine
    platform.machine = lambda: "x86_64"
    try:
        code, out = call(lab.setup().apply, ADDRESS)
    finally:
        platform.machine = original
    assert code == 2 and ("Apple silicon" in out or "Rosetta" in out), out
    assert not (lab.root / ".stick-setup").exists() and not lab.env_dir("voice_env").exists()


# ---- existing files this tool does not own ---------------------------------------------------------------------------------

@with_lab()
def existing_environments_files_and_configuration_are_preserved_and_conflicts_stop_the_setup_first(lab):
    own = lab.env_dir("voice_env")
    own.mkdir(parents=True)
    (own / "pyvenv.cfg").write_text("home = elsewhere\n")
    (own / "sentinel.txt").write_text("mine")
    folder = lab.root / "tts-models"
    folder.mkdir()
    (folder / "a.bin").write_bytes(b"someone else's file")                                         # same name, different content
    (folder / "c.json").write_bytes(MODEL_FILES["tts"]["c.json"])                                   # same name, identical content
    (lab.root / "config").mkdir(exist_ok=True)
    conf = "# my own configuration\n[server]\nadvertise_host = %s\n" % ADDRESS
    (lab.root / "config" / "host.conf").write_text(conf)
    before = snapshot(own, folder)
    code, out = call(lab.setup().apply, None)
    assert code == 2 and "a different file already exists" in out and "Stopped before changing anything" in out, out
    assert "Stopped before changing anything" in out and snapshot(own, folder) == before
    assert not lab.env_dir("tools_env").exists() and not (lab.root / ".stick-setup").exists()
    (folder / "a.bin").unlink()                                                                    # the user resolves the conflict
    code, out = call(lab.setup().apply, None)
    assert code == 0, out
    assert (own / "sentinel.txt").read_text() == "mine" and not (own / sm.OWNED).exists() and not (own / sm.PARTIAL).exists()   # kept, not adopted
    assert (lab.root / "config" / "host.conf").read_text() == conf
    code, out = call(lab.setup().remove, True)
    assert code == 0
    assert own.is_dir() and (own / "sentinel.txt").is_file()                                       # never ours: never deleted
    assert (folder / "c.json").read_bytes() == MODEL_FILES["tts"]["c.json"]                         # pre-existing identical file: not ours either
    assert not (folder / "a.bin").exists() and not (folder / "b.bin").exists()                     # the ones it downloaded are removed


# ---- concurrency ------------------------------------------------------------------------------------------------------------

@with_lab()
def a_running_setup_blocks_a_second_one_and_a_dead_ones_lock_is_taken_over(lab):
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        state_dir = lab.root / ".stick-setup"
        state_dir.mkdir()
        (state_dir / "lock").write_text(json.dumps({"pid": sleeper.pid, "started": "now"}))
        before = snapshot(lab.root / "tts-models", lab.env_dir("voice_env"))
        code, out = call(lab.setup().apply, ADDRESS)
        assert code == 3 and "already running" in out and "delete" in out, out
        assert not lab.env_dir("voice_env").exists() and snapshot(lab.root / "tts-models", lab.env_dir("voice_env")) == before
    finally:
        sleeper.kill()
        sleeper.wait()
    code, out = call(lab.setup().apply, ADDRESS)                                                    # its owner is gone now
    assert code == 0 and "no longer running" in out, out


@with_lab()
def two_setups_started_together_end_in_one_consistent_installation(lab):
    first = lab.spawn_cli("apply", "--advertise-host", ADDRESS)
    second = lab.spawn_cli("apply", "--advertise-host", ADDRESS)
    outs = [p.communicate(timeout=300)[0] for p in (first, second)]
    assert sorted(p.returncode for p in (first, second))[0] == 0 and all(p.returncode in (0, 3) for p in (first, second)), outs
    plan = lab.setup().build_plan()
    assert all(s["status"] in ("done", "preserved") for s in plan["steps"]), plan["steps"]
    assert runs(lab.env_dir("voice_env")) and not (lab.root / ".stick-setup" / "lock").exists()


# ---- removal ----------------------------------------------------------------------------------------------------------------

@with_lab()
def removal_deletes_only_what_the_setup_created_and_keeps_user_data_credentials_and_changed_files(lab):
    assert call(lab.setup().apply, ADDRESS)[0] == 0
    decoys = {"data/panel.db": b"settings", "secrets/device_key.txt": b"not a real key", "recordings/a.wav": b"audio", "tts-models/my-notes.txt": b"notes",
              "backups/old.tar": b"backup", "home/.cache/shared/model.bin": b"cache"}
    for rel, data in decoys.items():
        target = (lab.root if not rel.startswith("home/") else lab.t) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    changed = lab.root / "tts-models" / "b.bin"
    changed.write_bytes(b"edited by the user")                                                    # no longer what was downloaded
    before = snapshot(lab.root / "data", lab.root / "secrets", lab.root / "recordings", lab.root / "backups", lab.t / "home")
    plan_before = snapshot(lab.root)
    code, out = call(lab.setup().remove, False)                                                    # the plan is read-only
    assert code == 0 and "PLAN (read-only" in out and "--apply" in out and snapshot(lab.root) == plan_before
    assert "changed since setup: NOT deleted" in out and "never touched" in out
    code, out = call(lab.setup().remove, True)
    assert code == 0, out
    for name in ("voice_env", "tools_env"):
        assert not lab.env_dir(name).exists(), name
    assert not (lab.root / "tts-models" / "a.bin").exists() and not (lab.root / "whisper-model").exists() and not (lab.root / ".stick-setup").exists()
    assert changed.read_bytes() == b"edited by the user" and (lab.root / "tts-models" / "my-notes.txt").read_bytes() == b"notes"
    assert (lab.root / "config" / "host.conf").is_file()
    assert snapshot(lab.root / "data", lab.root / "secrets", lab.root / "recordings", lab.root / "backups", lab.t / "home") == before


@with_lab()
def removal_refuses_forged_or_dangerous_paths_even_when_the_notes_and_markers_agree(lab):
    assert call(lab.setup().apply, ADDRESS)[0] == 0
    state_file = lab.root / ".stick-setup" / "state.json"
    state = json.loads(state_file.read_text())
    decoy = lab.t / "Someone's folder (important)"
    decoy.mkdir()
    (decoy / "pyvenv.cfg").write_text("fake")
    (decoy / "precious.txt").write_text("keep me")
    victims = {"voice_env": decoy, "tools_env": lab.t}                                              # a lookalike folder, and an ancestor of the project
    for key, path in victims.items():
        state["resources"][key]["path"] = str(path)
        (path / sm.OWNED).write_text(json.dumps({"install_id": state["install_id"], "key": key}))   # even a matching marker must not be enough
    state_file.write_text(json.dumps(state))
    real_voice = lab.env_dir("voice_env")
    code, out = call(lab.setup().remove, True)
    assert (decoy / "precious.txt").read_text() == "keep me" and lab.root.is_dir() and lab.t.is_dir(), out
    assert real_voice.is_dir()                                                                      # the real one is no longer in the notes: nothing to say it is ours
    link = lab.t / "link-to-decoy"
    link.symlink_to(decoy)
    assert not sm.safe_to_delete_tree(link, lab.root) and not sm.safe_to_delete_tree(lab.t, lab.root) and not sm.safe_to_delete_tree(lab.root, lab.root)
    real_home = Path.__dict__["home"]
    setattr(Path, "home", classmethod(lambda cls: lab.t / "home"))                              # a stand-in home folder: it, and "/", must be refused
    try:
        assert not sm.safe_to_delete_tree(lab.t / "home", lab.root) and not sm.safe_to_delete_tree(Path("/"), lab.root)
    finally:
        setattr(Path, "home", real_home)


# ---- read-only plan, entry point -------------------------------------------------------------------------------------------

@with_lab()
def the_plan_is_read_only_and_works_with_the_old_python_that_ships_with_the_command_line_tools(lab):
    before = snapshot(lab.t)
    done = lab.cli("plan")
    assert done.returncode == 0 and "PLAN (read-only: nothing has been changed)" in done.stdout, done.stderr
    assert snapshot(lab.t) == before and not (lab.root / ".stick-setup").exists()                    # not even bytecode
    as_json = json.loads(lab.cli("plan", "--json").stdout)
    assert as_json["read_only"] is True and any(d["from"].startswith(lab.server.base) for d in as_json["downloads"])
    old = Path("/usr/bin/python3")
    if old.exists() and subprocess.run([str(old), "-c", "import sys;sys.exit(0 if sys.version_info[:2]==(3,9) else 1)"], capture_output=True,
                                       env={"HOME": str(lab.t / "home"), "PATH": "/usr/bin:/bin"}).returncode == 0:
        project_before = snapshot(lab.root)
        done = subprocess.run([str(old), str(lab.root / "tools" / "setup_mac.py"), "plan"], capture_output=True, text=True, env={"HOME": str(lab.t / "home"), "PATH": "/usr/bin:/bin"}, timeout=120)
        assert done.returncode == 0, done.stderr
        assert snapshot(lab.root) == project_before         # (Apple's own Python 3.9 keeps its standard-library bytecode cache under ~/Library/Caches: that is the interpreter, not setup)


@with_lab()
def the_shell_entry_point_finds_a_python_or_explains_how_to_get_one_without_changing_anything(lab):
    script = lab.root / "setup-mac.sh"
    before = snapshot(lab.t)
    none = subprocess.run(["bash", str(script)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "STICK_SETUP_PYTHON_CANDIDATES": str(lab.t / "no-such-python")}, timeout=60)
    assert none.returncode == 2 and "Python 3.9 or newer" in none.stderr and "brew install python@3.14" in none.stderr and "python.org" in none.stderr
    assert snapshot(lab.t) == before
    found = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": str(lab.t / "home"), "STICK_SETUP_PYTHON_CANDIDATES": sys.executable})
    assert found.returncode == 0 and "PLAN (read-only" in found.stdout, found.stderr          # no argument means plan


if __name__ == "__main__":
    failures = 0
    for fn in CASES:
        try:
            fn()
            print("ok   " + fn.__name__, flush=True)
        except Exception as error:                      # noqa: BLE001
            import traceback
            failures += 1
            print("FAIL %s: %s: %s" % (fn.__name__, type(error).__name__, error), flush=True)
            traceback.print_exc()
    print("%d/%d setup cases passed" % (len(CASES) - failures, len(CASES)))
    sys.exit(1 if failures else 0)
