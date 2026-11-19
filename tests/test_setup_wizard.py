"""Tests for the guided setup (setup.sh -> tools/setup_wizard.py), driven like a person at the keyboard.

Everything is temporary and synthetic: a COPY of the project in a folder whose name has spaces and brackets; the Mac side prepared by
the REAL tools/setup_mac.py with a tiny local wheel and a local model server (tests/test_setup_mac.py's stand-ins, no internet);
the REAL Stick tools against the simulated Stick (tests/fixtures/fake_stick_device.py); and, as "the server", an isolated process
(tests/fixtures/fake_assistant_server.py) running the real DeviceLink, started through a stand-in for macOS `open` exactly where the
wizard would open the launcher in Terminal. Claude and ffmpeg are stub executables. Progress notes go to a temporary folder outside
the copy. The isolation guard is active in every process."""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import test_setup_mac as tsm
import test_stick_backup as tb
import test_stick_setup as ts

ROOT = tb.ROOT
FAKE_SERVER = ROOT / "tests" / "fixtures" / "fake_assistant_server.py"          # stands in for ANOTHER installation's server
BOOT_SIMULATOR = ROOT / "tests" / "fixtures" / "stick_boot_simulator.py"
COPY = ["stick_config.py", "setup.sh", "config/host.example.conf", "Start Voice Assistant.command",
        "tools/setup_wizard.py", "tools/setup_mac.py", "tools/stick_backup.py", "tools/stick_restore.py", "tools/stick_install.py",
        "tools/stick_setup.py", "tools/deploy_stick.sh"]
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WizardLab:
    """A temporary project copy, a simulated Stick, stub Claude/ffmpeg, a stand-in `open`, and private progress notes elsewhere."""

    def __init__(self, scenario="fresh", **stick_state):
        self.stick = ts.Lab(scenario=scenario, key=None, server=False, **stick_state)
        if scenario == "fresh":
            self.stick.set(nvs={"boot_option": 1, "ssid0": tb.SSID, "pswd0": tb.PASSWORD})      # Wi-Fi saved with UIFlow Setup
        self.t = Path(self.stick.tmp.name).resolve()
        self.root = self.t / "Voice Assistant (copy) [wizard] v1"
        for rel in COPY:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.root / rel)
        shutil.copytree(ROOT / "stick", self.root / "stick")
        for module in ROOT.glob("*.py"):                                         # the REAL server, secured (docs/security.md)
            shutil.copy2(module, self.root / module.name)
        shutil.copytree(ROOT / "panel", self.root / "panel")
        self.stubs = self.t / "stubs"
        self.stubs.mkdir()
        (self.stubs / "mlx_whisper.py").write_text("def transcribe(path, **k):\n    return {'text': 'stand-in'}\n")
        self.boots = self.t / "boots.log"
        wheel = tsm.build_wheel(self.t)
        lock = "tinypkg @ %s \\\n    --hash=sha256:%s\n" % (wheel.as_uri(), tsm.hashlib.sha256(wheel.read_bytes()).hexdigest())
        req = self.root / "requirements"
        req.mkdir()
        for name in ("server", "stick-tools"):
            (req / (name + ".lock")).write_text(lock)
        (req / "setup.json").write_text(json.dumps({"schema": 1, "environments": [
            {"id": e, "title": "Test environment " + e, "python": tsm.PYVER, "lock": "requirements/%s.lock" % ("server" if e == "voice_env" else "stick-tools"),
             "smoke": ["tinypkg"], "approx_bytes": 1000} for e in ("voice_env", "tools_env")]}))
        manifest = {key: {"repo": "test/" + key, "revision": tsm.REVISION, "folder": folder,
                          "files": [{"name": n, "bytes": len(d), "sha256": tsm.hashlib.sha256(d).hexdigest()} for n, d in tsm.MODEL_FILES[key].items()]}
                    for key, folder in (("tts", "models_dir"), ("whisper", "whisper_dir"))}
        (req / "models.json").write_text(json.dumps(manifest))
        self.models = tsm.ModelServer()
        self.device_port, self.panel_port = free_port(), free_port()
        self.state = self.t / "notes" / "progress.json"
        self.home = self.t / "home"
        self.home.mkdir()
        self.bin = self.t / "fakebin"
        self.bin.mkdir()
        for name in ("claude", "ffmpeg"):
            (self.bin / name).write_text("#!/bin/sh\nexit 0\n")
            (self.bin / name).chmod(0o755)
        self.open_log = self.t / "open-calls.log"
        self.servers = self.t / "server-pids"
        (self.bin / "open").write_text(f"""#!/bin/bash
# A stand-in for macOS `open`: records the call; for `open -a Terminal <launcher>` it starts that folder's REAL server (with stand-in AI),
# as Terminal would through the launcher, and the simulated Stick's network side.
echo "$*" >> "{self.open_log}"
if [ "$1" = "-a" ] && [ "$2" = "Terminal" ]; then
    export PYTHONPATH="{isolation_guard.child_pythonpath(str(self.stubs))}"
    "{sys.executable}" -u "$(dirname "$3")/audio_server.py" >> "{self.t / 'server.log'}" 2>&1 &
    echo $! >> "{self.servers}"
    "{sys.executable}" "{BOOT_SIMULATOR}" --device-port {self.device_port} --device-state "{self.stick.state_path}" \\
        --flash "{self.stick.flash}" --log "{self.boots}" >> "{self.t / 'simulator.log'}" 2>&1 &
    echo $! >> "{self.servers}"
fi
""")
        (self.bin / "open").chmod(0o755)
        self.procs = []

    def env(self, **more):
        env = {k: v for k, v in self.stick.env().items() if k not in ("STICK_CONFIG", "STICK_ADVERTISE_HOST")}
        env.update({"PYTHONPATH": isolation_guard.child_pythonpath(), "STICK_DATA_DIR": str(self.stick.data),
                    "STICK_SECRETS_DIR": str(self.stick.secrets)})                     # the guard, and temporary data and secrets, in every child
        env.update({"STICK_BIND_HOST": "127.0.0.1", "STICK_RECORDINGS_DIR": str(self.t / "recordings"), "STICK_CLAUDE": str(self.bin / "claude")})
        env.update({"HOME": str(self.home), "STICK_WIZARD_STATE": str(self.state), "STICK_DEVICE_PORT": str(self.device_port),
                    "STICK_PANEL_PORT": str(self.panel_port), "STICK_WIZARD_SERVER_WAIT_S": "40",
                    "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin"})
        env.update(more)
        return env

    def mac_options(self, python=sys.executable):
        return [f"--mac-option=--python={tsm.PYVER}={python}", f"--mac-option=--models-base={self.models.base}"]

    def wizard(self, *args, **env):
        return Driver([sys.executable, str(self.root / "tools" / "setup_wizard.py"), *args], self.env(**env), self.t)

    def prepare_mac(self):
        done = subprocess.run([sys.executable, str(self.root / "tools" / "setup_mac.py"), "apply", "--advertise-host", "192.0.2.10",
                               "--python", f"{tsm.PYVER}={sys.executable}", "--models-base", self.models.base],
                              capture_output=True, text=True, env=self.env(), timeout=600, stdin=subprocess.DEVNULL)
        assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]

    def start_server(self, label=None):
        """This folder's REAL server (label None), or a stand-in for ANOTHER installation's server named by label."""
        if label is None:
            command = [sys.executable, "-u", str(self.root / "audio_server.py")]
            env = self.env(PYTHONPATH=isolation_guard.child_pythonpath(str(self.stubs)))
        else:
            command = [sys.executable, str(FAKE_SERVER), "--panel-port", str(self.panel_port), "--device-port", str(self.device_port),
                       "--data", str(self.t / "server-data"), "--secrets", str(self.stick.secrets), "--device-state", str(self.stick.state_path),
                       "--flash", str(self.stick.flash), str(label)]
            env = self.env()
        proc = subprocess.Popen(command, env=env, stdout=open(self.t / "early-server.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(proc)
        wait_listening(self.panel_port, timeout=60)
        return proc

    def opened(self):
        return self.open_log.read_text().splitlines() if self.open_log.exists() else []

    def notes(self):
        return json.loads(self.state.read_text())

    def close(self):
        pids = [int(p) for p in self.servers.read_text().split()] if self.servers.exists() else []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for proc in self.procs:
            proc.terminate()
            proc.wait(timeout=10)
        self.models.close()
        self.stick.close()


def wait_listening(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError(f"nothing listened on {port}")


class Driver:
    """The wizard as a person sees it: read until it asks (a prompt ends with '» '), check what it said, answer."""

    def __init__(self, command, env, cwd):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd)
        self.raw = b""
        self.seen = 0

    def read_until_prompt(self, timeout=600):
        deadline = time.time() + timeout
        fd = self.proc.stdout.fileno()
        while time.time() < deadline:
            if self.raw.endswith("» ".encode()) and len(self.raw) > self.seen:
                chunk = self.raw[self.seen:].decode(errors="replace")
                self.seen = len(self.raw)
                return chunk
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                data = os.read(fd, 65536)
                if not data:
                    raise AssertionError("the wizard ended while a question was expected:\n" + self.raw[self.seen:].decode(errors="replace")[-3000:])
                self.raw += data
        raise AssertionError("no question within the time limit:\n" + self.raw[self.seen:].decode(errors="replace")[-3000:])

    def step(self, expect, answer):
        chunk = self.read_until_prompt()
        assert expect in chunk, (expect, chunk[-3000:])
        self.proc.stdin.write((answer + "\n").encode())
        self.proc.stdin.flush()
        return chunk

    def finish(self, timeout=600):
        self.proc.stdin.close()
        rest, _ = self.proc.communicate(timeout=timeout)
        self.raw += rest
        return self.proc.returncode, self.raw.decode(errors="replace")


def lab_case(fn=None, **kw):
    def wrap(f):
        def run():
            lab = WizardLab(**kw)
            try:
                f(lab)
            finally:
                lab.close()
        run.__name__ = f.__name__
        return case(run)
    return wrap(fn) if fn else wrap


def secret_free(lab, *texts):
    key = lab.stick.mac_key_text()
    everything = list(texts) + ([lab.state.read_text()] if lab.state.exists() else [])
    tb.no_secrets(*everything)
    for text in everything:
        assert not key or key not in text, "a device key leaked"


# ---- the default is a read-only explanation ------------------------------------------------------------------------------------
@lab_case
def the_default_explains_and_plans_without_any_change(lab):
    done = subprocess.run(["bash", str(lab.root / "setup.sh")], capture_output=True, text=True, timeout=120,
                          env=lab.env(STICK_SETUP_PYTHON_CANDIDATES=sys.executable), cwd=str(lab.t))
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "התקנה מודרכת" in out and "תצוגה מקדימה לשימוש לא-מסחרי" in out and "UIFlow2 V2.4.8" in out, out
    assert "Claude Code" in out and "3.4GB" in out and "לא נבדקה עדיין על חומרה אמיתית" in out
    assert "שום דבר לא הורד, לא חובר" in out and "./setup.sh run" in out
    status = subprocess.run([sys.executable, str(lab.root / "tools" / "setup_wizard.py"), "status"], capture_output=True, text=True,
                            env=lab.env(), timeout=300, cwd=str(lab.t), stdin=subprocess.DEVNULL)
    assert status.returncode == 0 and "נבדק עכשיו" in status.stdout and "Test environment voice_env" in status.stdout, status.stdout + status.stderr
    assert not lab.state.exists() and lab.stick.calls() == [] and lab.opened() == []
    assert not (lab.root / "config" / "host.conf").exists() and not (lab.root / "voice-env").exists()


@lab_case
def progress_notes_are_refused_inside_the_project(lab):
    done = subprocess.run([sys.executable, str(lab.root / "tools" / "setup_wizard.py"), "run"], capture_output=True, text=True,
                          env=lab.env(STICK_WIZARD_STATE=str(lab.root / "data" / "notes.json")), timeout=60, cwd=str(lab.t), stdin=subprocess.DEVNULL)
    assert done.returncode != 0 and "outside the project folder" in done.stdout + done.stderr


# ---- a fresh Mac and a fresh Stick: stop, resume, finish; then a run on the finished setup ----------------------------------------
@lab_case
def a_fresh_setup_can_stop_resume_finish_and_a_rerun_changes_nothing(lab):
    w = lab.wizard("run", *lab.mac_options())
    w.step("התקנה מודרכת", "כ")
    chunk = w.step("הקלידו את הכתובת", "192.0.2.10")
    assert "Test environment voice_env" in chunk and "בלי sudo" in chunk, chunk
    w.step("להתחיל את הכנת ה-Mac", "כ")
    w.step("התחברתם ל-Claude Code", "כ")
    w.step("האם ה-Wi-Fi כבר שמור", "כ")
    w.step("הקלידו את היציאה", lab.stick.port)
    w.step("לזהות את ה-Stick", "כ")
    chunk = w.step("להתקין בדיוק את הקבצים האלה", "ע")                       # the person stops before any change
    assert "ייווצר: /flash/mic.py" in chunk and "יוחלף: /flash/main.py" in chunk, chunk
    code, out = w.finish()
    assert code == 3 and "ההתקדמות נשמרה" in out, out[-1500:]
    notes = lab.notes()["stages"]
    assert [notes[k]["status"] for k in ("mac", "claude", "wifi", "identify", "files")] == ["done", "user-confirmed", "user-confirmed", "done", "stopped"]
    assert lab.stick.writes() == [] and lab.stick.backups() == [] and (lab.root / "config" / "host.conf").exists()
    assert oct(lab.state.stat().st_mode & 0o777) == "0o600" and oct(lab.state.parent.stat().st_mode & 0o777) == "0o700"

    early = lab.start_server()                                               # the person had started the server before the key existed
    w = lab.wizard("run", *lab.mac_options())
    w.step("התקנה מודרכת", "כ")
    chunk = w.step("הקלידו את היציאה", lab.stick.port)                        # Mac, Claude and Wi-Fi are rechecked, not asked again
    assert "ה-Mac מוכן" in chunk and "סימנתם קודם" in chunk and "Wi-Fi שמור" in chunk, chunk
    w.step("לזהות את ה-Stick", "כ")
    w.step("להתקין בדיוק את הקבצים האלה", "כ")
    w.step("לגבות עכשיו", "כ")
    w.step("להתקין את המפתח", "כ")
    chunk = w.read_until_prompt()
    assert "עצרתם את השרת" in chunk and "לא סוגר תהליכים" in chunk, chunk
    early.terminate()                                                        # the person stops it in its window
    early.wait(timeout=10)
    w.proc.stdin.write("כ\n".encode())
    w.proc.stdin.flush()
    w.step("להפעיל את השרת", "כ")
    w.step("להפעיל הפעלה אוטומטית", "כ")
    w.step("להפעיל מחדש ולאמת", "כ")
    chunk = w.step("No speech", "כ")
    assert "CONFIRMED" in chunk and "OBSERVED" in chunk, chunk
    w.step("שמעתם תשובה", "כ")
    w.step("המסך התעמעם", "כ")
    code, out = w.finish()
    assert code == 0 and "אימות תפעולי (נמדד)" in out and "אישור שלכם, לא נמדד" in out and "תצוגה מקדימה לשימוש לא-מסחרי" in out, out[-2500:]
    assert "מתאם בזמן, לא זיהוי ישיר" in out
    assert lab.stick.nvs()["boot_option"] == 0 and (lab.stick.flash / "device_key.txt").read_text().strip() == lab.stick.mac_key_text()
    assert len(lab.stick.backups()) == 1 and len([c for c in lab.opened() if "-a Terminal" in c]) == 1
    notes = lab.notes()
    assert notes["operational"]["verified"] is True and notes["voice_check"]["user_confirmed"] == {"silent": True, "answer": True, "sleep": True}
    [boot] = [json.loads(line) for line in lab.boots.read_text().splitlines()]            # the secured server, over HTTP, as the Stick sees it
    assert boot == {"boot": 1, "sync": 200, "audio_without_key": 401, "audio": 200, "status": 200, "state": "no_speech"}, boot
    secret_free(lab, out)

    lab.stick.log.write_text("")
    w = lab.wizard("run", *lab.mac_options())
    w.step("התקנה מודרכת", "כ")
    w.step("הקלידו את היציאה", lab.stick.port)
    w.step("לזהות את ה-Stick", "כ")
    chunk = w.step("להפעיל מחדש ולאמת", "כ")                                  # files, key, server and auto-start are rechecked, not redone
    assert "כבר מותקנים ותואמים" in chunk and "כבר יש את המפתח" in chunk and "פועל וטען מפתח" in chunk, chunk
    for prompt in ("No speech", "שמעתם תשובה", "המסך התעמעם"):
        w.step(prompt, "כ")
    code, out = w.finish()
    assert code == 0, out[-1500:]
    assert lab.stick.writes() == [] and lab.stick.nvs_writes() == [] and len(lab.stick.backups()) == 1
    assert len([c for c in lab.opened() if "-a Terminal" in c]) == 1
    secret_free(lab, out)


# ---- what stops it ------------------------------------------------------------------------------------------------------------
@lab_case
def missing_prerequisites_stop_before_any_change_with_guidance(lab):
    spec = lab.root / "requirements" / "setup.json"
    spec.write_text(spec.read_text().replace(f'"python": "{tsm.PYVER}"', '"python": "3.99"'))   # a Python this Mac does not have
    w = lab.wizard("run", f"--mac-option=--models-base={lab.models.base}", STICK_FFMPEG="/nonexistent/ffmpeg")
    w.step("התקנה מודרכת", "כ")
    code, out = w.finish()
    assert code == 3 and "חסרים כלים" in out and "ffmpeg" in out and "brew install" in out and "התקינו את מה שחסר" in out, out[-2000:]
    assert not (lab.root / "config" / "host.conf").exists() and not (lab.root / "voice-env").exists()
    assert lab.stick.calls() == [] and lab.notes()["stages"]["mac"]["status"] == "stopped"


@lab_case(scenario="current")
def a_port_held_by_another_program_or_installation_stops_and_nothing_is_killed(lab):
    lab.prepare_mac()
    (lab.stick.flash / "device_config.json").write_text(json.dumps({"v": 1, "server_host": "192.0.2.10", "server_port": lab.device_port},
                                                                   indent=2, sort_keys=True) + "\n")     # installed for THIS Mac's settings
    k = ts.random_key()
    lab.stick.mac_key(k)
    (lab.stick.flash / "device_key.txt").write_text(k + "\n")
    other = subprocess.Popen([sys.executable, "-m", "http.server", str(lab.device_port), "--bind", "127.0.0.1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=lab.env(), start_new_session=True)
    lab.procs.append(other)
    wait_listening(lab.device_port)
    w = lab.wizard("run", *lab.mac_options())
    w.step("התקנה מודרכת", "כ")
    w.step("התחברתם ל-Claude Code", "כ")
    w.step("האם ה-Wi-Fi כבר שמור", "כ")
    w.step("הקלידו את היציאה", lab.stick.port)
    w.step("לזהות את ה-Stick", "כ")
    code, out = w.finish()
    assert code == 3 and "תהליך אחר תופס יציאה" in out and f"PID {other.pid}" in out and "לא סוגר אותו" in out, out[-2000:]
    assert other.poll() is None and lab.opened() == []
    other.terminate()
    other.wait(timeout=10)
    elsewhere = lab.start_server(label=lab.t / "Another Install" / "audio_server.py")   # an assistant from another folder
    w = lab.wizard("run", *lab.mac_options())
    w.step("התקנה מודרכת", "כ")
    w.step("הקלידו את היציאה", lab.stick.port)
    w.step("לזהות את ה-Stick", "כ")
    code, out = w.finish()
    assert code == 3 and "Another Install" in out and "מתיקייה אחרת" in out, out[-2000:]
    assert elsewhere.poll() is None and lab.opened() == [] and lab.stick.writes() == []


@lab_case(faults={"cp_to_fail": ["/flash/main.py"]})
def a_failed_stage_stops_the_run_and_later_stages_never_start(lab):
    lab.prepare_mac()
    w = lab.wizard("run", *lab.mac_options())
    w.step("התקנה מודרכת", "כ")
    w.step("התחברתם ל-Claude Code", "כ")
    w.step("האם ה-Wi-Fi כבר שמור", "כ")
    w.step("הקלידו את היציאה", lab.stick.port)
    w.step("לזהות את ה-Stick", "כ")
    w.step("להתקין בדיוק את הקבצים האלה", "כ")
    w.step("לגבות עכשיו", "כ")
    code, out = w.finish()
    assert code == 1 and "ההתקנה לא הסתיימה" in out and "נכשל" in out, out[-2000:]
    notes = lab.notes()["stages"]
    assert notes["files"]["status"] == "failed" and "key" not in notes
    assert lab.stick.mac_key_text() is None and not (lab.stick.flash / "device_key.txt").exists() and lab.opened() == []
    assert lab.stick.nvs()["boot_option"] == 1


@lab_case
def a_stick_without_wifi_is_sent_back_to_uiflow_setup(lab):
    lab.prepare_mac()
    lab.stick.set(nvs={"boot_option": 1})
    w = lab.wizard("run", *lab.mac_options())
    w.step("התקנה מודרכת", "כ")
    w.step("התחברתם ל-Claude Code", "כ")
    chunk = w.step("האם ה-Wi-Fi כבר שמור", "כ")                              # the person says yes, but the Stick says otherwise
    assert "הסיסמה לחיבור ה-USB" in chunk and "BtnA" in chunk
    w.step("הקלידו את היציאה", lab.stick.port)
    w.step("לזהות את ה-Stick", "כ")
    code, out = w.finish()
    assert code == 3 and "אין Wi-Fi שמור" in out and "machine.reset()" in out, out[-1500:]
    assert lab.notes()["stages"]["wifi"]["status"] == "needed" and lab.stick.writes() == []


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:1500])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:1500])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
