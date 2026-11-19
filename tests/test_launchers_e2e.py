"""Scripted end-to-end checks of both desktop launchers against a REAL audio_server.py running in isolation.

This is NOT a Finder double-click. What it proves is what the launcher scripts do when zsh runs them; what it cannot prove is how Finder, Terminal.app
and macOS permissions behave when a person double-clicks the file (see docs/launchers.md, "Physical checks").

Isolation: the project is a copy in a temporary folder whose name has spaces, brackets and parentheses; data/secrets/recordings are temporary; ports are
ephemeral on 127.0.0.1; HOME is temporary. Stand-ins: `claude` (a script), mlx_whisper (a stub module) and the TTS (a tiny WAV writer). The launchers
run under neutral names (the guard refuses anything named like the real ones), and `open` is a recording stand-in that, for `open -a Terminal <launcher>`,
runs the neutral copy of that launcher in the background (what Terminal would do) and, for a URL, only records it (no browser is opened).

Run with the server's interpreter (needs numpy/scipy): voice-env/bin/python tests/test_launchers_e2e.py
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stick_config as sc  # noqa: E402

CASES = []
SKIP = {"data", "secrets", "recordings", "voice-env", "tts-env312", "tts-models", ".git", "__pycache__", "tests", "design", "docs", "tools", "stick", "hooks", "config"}
START, PANEL = "Start Voice Assistant.command", "Voice Assistant Panel.command"


def case(fn):
    CASES.append(fn)
    return fn


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def wait_for(predicate, seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.2)
    return False


class Lab:
    """One isolated installation: project copy, stand-ins, temporary data, two free ports, and the neutral launcher copies."""

    def __init__(self, with_config=True):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()                  # the launchers resolve symlinks (/var -> /private/var): process matching must use the same spelling
        self.root =t / "Voice Assistant (copy) [e2e] v1"
        self.root.mkdir()
        for entry in ROOT.iterdir():
            if entry.name in SKIP or entry.name.startswith(".") or "backup" in entry.name:
                continue
            (shutil.copytree if entry.is_dir() else shutil.copy2)(entry, self.root / entry.name)
        for sub in ("stick", "config"):                                      # the server imports stick_config; the Stick's files are not needed
            (self.root / sub).mkdir(exist_ok=True)
        shutil.copy2(ROOT / "config" / "host.example.conf", self.root / "config" / "host.example.conf")
        for name in (START, PANEL):
            shutil.copy2(ROOT / name, self.root / name)                     # present (the panel launcher checks it exists) but never executed under this name
        self.start_copy, self.panel_copy = self.root / "server-launcher-under-test.command", self.root / "panel-launcher-under-test.command"
        shutil.copy2(ROOT / START, self.start_copy)
        shutil.copy2(ROOT / PANEL, self.panel_copy)
        self.device_port, self.panel_port = free_port(), free_port()
        self.home = t / "home"
        self.home.mkdir()
        self.bin = t / "fake bin"
        self.bin.mkdir()
        self.opened, self.terminal_log = t / "opened.log", t / "terminal.log"
        self.opened.write_text("")
        self.write_stand_ins(t)
        self.key = "synthetic-e2e-device-key-0123456789abcdef"
        (t / "secrets").mkdir(mode=0o700)
        (t / "secrets" / "device_key").write_text(self.key + "\n")
        os.chmod(t / "secrets" / "device_key", 0o600)
        if with_config:
            self.write_config()
        self.children = []

    def write_stand_ins(self, t):
        py = sys.executable
        claude = self.bin / "claude"
        claude.write_text(f'#!{py}\nimport sys\nsys.stdin.read()\nprint("stand-in answer")\n')
        stubs = t / "stubs"
        stubs.mkdir()
        (stubs / "mlx_whisper.py").write_text('def transcribe(path, **kw):\n    return {"text": "stand-in transcript"}\n')
        tts = t / "tts-env312"
        (tts / "bin").mkdir(parents=True)
        (t / "tts.py").write_text('import argparse, math, struct, wave\np = argparse.ArgumentParser()\n'
                                  'for a in ("--model", "--config", "--phonikud", "--output", "--length-scale", "--text"):\n    p.add_argument(a)\n'
                                  'a = p.parse_args()\nw = wave.open(a.output, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)\n'
                                  'w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(600))); w.close()\n')
        (tts / "bin" / "python").write_text(f'#!/bin/sh\nshift\nexec {py} {t / "tts.py"} "$@"\n')
        voice = t / "voice-env"
        (voice / "bin").mkdir(parents=True)
        (voice / "bin" / "python").write_text(f'#!/bin/sh\nexec {py} "$@"\n')   # the server's interpreter is this test's own (it has numpy/scipy)
        (self.bin / "open").write_text(f'''#!/bin/sh
echo "$*" >> "{self.opened}"
if [ "$1" = "-a" ] && [ "$2" = "Terminal" ]; then
    # what Terminal would do with a .command file: run it (here the neutral copy of the same launcher), detached, output to a log
    dir=$(dirname "$3")
    nohup zsh "$dir/server-launcher-under-test.command" >> "{self.terminal_log}" 2>&1 < /dev/null &
fi
exit 0
''')
        for f in (claude, voice / "bin" / "python", tts / "bin" / "python", self.bin / "open"):
            f.chmod(0o755)
        self.stubs, self.tts, self.voice = stubs, tts, voice

    def write_config(self, extra=""):
        conf = self.root / "config" / "host.conf"
        conf.parent.mkdir(exist_ok=True)
        conf.write_text(f"[server]\nadvertise_host = 192.0.2.10\nbind_host = 127.0.0.1\ndevice_port = {self.device_port}\npanel_port = {self.panel_port}\n"
                        f"[paths]\nclaude = {self.bin / 'claude'}\nvoice_env = {self.voice}\ntts_env = {self.tts}\n{extra}")

    def env(self, **more):
        env = {k: v for k, v in os.environ.items() if not k.startswith("STICK_") and k != "MPREMOTE"}
        python_dir = str(Path(shutil.which("python3")).parent)
        env.update({"HOME": str(self.home), "PATH": f"{self.bin}:{python_dir}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    "PYTHONPATH": isolation_guard.child_pythonpath(str(self.stubs), str(self.root)),
                    "STICK_DATA_DIR": str(Path(self.tmp.name) / "data"), "STICK_SECRETS_DIR": str(Path(self.tmp.name) / "secrets"),
                    "STICK_RECORDINGS_DIR": str(Path(self.tmp.name) / "recordings"),
                    # explicit, distinct, non-live ports (the same ones the lab's configuration names): a launcher left to its defaults would probe the
                    # running assistant, and the isolation guard refuses a port-aware script started without them
                    "STICK_DEVICE_PORT": str(self.device_port), "STICK_PANEL_PORT": str(self.panel_port)})
        env.update(more)
        return env

    def run(self, script, timeout=120, **env):
        return subprocess.run(["zsh", str(script)], capture_output=True, text=True, env=self.env(**env), stdin=subprocess.DEVNULL, timeout=timeout)

    def spawn(self, script, **env):
        log = open(Path(self.tmp.name) / f"{script.stem}.log", "w")
        proc = subprocess.Popen(["zsh", str(script)], stdout=log, stderr=subprocess.STDOUT, env=self.env(**env), stdin=subprocess.DEVNULL)
        self.children.append((proc, log))
        return proc

    def servers(self):
        """PIDs of Python processes started with this project's audio_server.py."""
        out = subprocess.run(["pgrep", "-f", re.escape(str(self.root / "audio_server.py")) + "$"], capture_output=True, text=True).stdout.split()
        return [int(p) for p in out if int(p) != os.getpid()]

    def panel_json(self):
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.panel_port}/api/status", timeout=3))

    def stop(self):
        subprocess.run(["pkill", "-TERM", "-f", re.escape(str(self.root / "audio_server.py")) + "$"], capture_output=True)
        for proc, log in self.children:
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
        wait_for(lambda: not self.servers(), 15)
        self.tmp.cleanup()


def with_lab(**kw):
    def wrap(fn):
        def run():
            lab = Lab(**kw)
            try:
                fn(lab)
            finally:
                lab.stop()
        run.__name__ = fn.__name__
        return case(run)
    return wrap


@with_lab()
def the_server_launcher_starts_the_real_server_when_none_is_running_and_the_server_answers_with_the_stand_ins(lab):
    assert lab.servers() == []
    proc = lab.spawn(lab.start_copy)
    assert wait_for(lambda: listening(lab.device_port) and listening(lab.panel_port)), (Path(lab.tmp.name) / "server-launcher-under-test.log").read_text()
    assert len(lab.servers()) == 1
    auth = {"X-Device-Key": lab.key}                                                                            # every LAN route needs the device key
    request = urllib.request.Request(f"http://127.0.0.1:{lab.device_port}/audio", data=bytes(192000), headers=auth)  # real pipeline, stand-ins
    job = json.load(urllib.request.urlopen(request, timeout=10))["id"]
    assert wait_for(lambda: json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{lab.device_port}/status?id={job}",
                                                                                     headers=auth), timeout=5))["state"] != "pending", 30)
    assert proc.poll() is None
    assert lab.panel_json()["key_provisioned"] in (True, False)
    assert not (ROOT / "data").exists() or (ROOT / "data") != Path(lab.tmp.name)


@with_lab()
def reopening_while_the_correct_server_runs_starts_no_second_instance_from_either_launcher(lab):
    lab.spawn(lab.start_copy)
    assert wait_for(lambda: listening(lab.device_port) and listening(lab.panel_port))
    before = lab.servers()
    assert len(before) == 1
    second = lab.run(lab.start_copy)                                           # the same launcher again
    assert second.returncode == 0 and "כבר פועל" in second.stdout, (second.stdout, second.stderr)
    assert lab.servers() == before
    third = lab.run(lab.panel_copy)                                            # the panel launcher: opens the loopback panel, starts nothing
    assert third.returncode == 0, (third.stdout, third.stderr)
    assert lab.servers() == before
    opened = lab.opened.read_text().split()
    assert opened == [f"http://127.0.0.1:{lab.panel_port}/"], opened             # the configured loopback URL, and no `open -a Terminal`
    assert not lab.terminal_log.exists()


@with_lab()
def the_panel_launcher_starts_the_server_through_the_regular_launcher_once_then_opens_the_configured_url(lab):
    assert lab.servers() == []
    done = lab.run(lab.panel_copy, timeout=120)
    assert done.returncode == 0, (done.stdout, done.stderr, lab.terminal_log.read_text() if lab.terminal_log.exists() else "")
    lines = lab.opened.read_text().splitlines()
    assert lines[0].startswith("-a Terminal ") and lines[0].endswith(START), lines          # asked Terminal to run the regular launcher
    assert lines[-1] == f"http://127.0.0.1:{lab.panel_port}/", lines
    assert len(lab.servers()) == 1
    again = lab.run(lab.panel_copy)                                            # a second press must not create a second server
    assert again.returncode == 0 and len(lab.servers()) == 1
    assert lab.opened.read_text().splitlines()[-1] == f"http://127.0.0.1:{lab.panel_port}/"
    assert lab.opened.read_text().count("-a Terminal") == 1


@with_lab()
def a_device_port_held_by_an_unrelated_process_is_reported_and_left_alone_by_both_launchers(lab):
    blocker = subprocess.Popen([sys.executable, "-c", f"import socket,time\ns=socket.socket()\ns.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
                                f"s.bind(('127.0.0.1',{lab.device_port}))\ns.listen(1)\ntime.sleep(120)"], stdin=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: listening(lab.device_port), 10)
        start = lab.run(lab.start_copy)
        assert start.returncode == 1 and str(lab.device_port) in start.stdout and "פורט" in start.stdout, (start.stdout, start.stderr)
        assert lab.servers() == [] and blocker.poll() is None
        panel = lab.run(lab.panel_copy)
        assert panel.returncode == 1 and "תהליך אחר" in panel.stdout and str(lab.device_port) in panel.stdout, (panel.stdout, panel.stderr)
        assert "השרת פועל" not in panel.stdout                                   # an unrelated listener is not "the server"
        assert lab.servers() == [] and blocker.poll() is None
        assert not lab.terminal_log.exists() and lab.opened.read_text() == ""
    finally:
        blocker.kill()
        blocker.wait()


@with_lab()
def an_unrelated_web_server_on_the_panel_port_is_not_mistaken_for_the_panel(lab):
    blocker = subprocess.Popen([sys.executable, "-m", "http.server", str(lab.panel_port), "--bind", "127.0.0.1", "--directory", lab.tmp.name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: listening(lab.panel_port), 10)
        done = lab.run(lab.panel_copy)                                           # nothing runs: it must start the server and then say the panel never came up
        assert lab.opened.read_text().splitlines()[0].startswith("-a Terminal "), lab.opened.read_text()
        assert f"http://127.0.0.1:{lab.panel_port}/" not in lab.opened.read_text().splitlines()[1:]
        assert done.returncode == 1 or "הפאנל" in done.stdout
    finally:
        blocker.kill()
        blocker.wait()


@with_lab(with_config=False)
def both_launchers_explain_a_missing_configuration_and_start_nothing(lab):
    for script in (lab.start_copy, lab.panel_copy):
        # without a configuration the launchers fall back to the default ports: pin free ones through the environment so that a REAL assistant running on this
        # Mac can never be what the launcher probes (the address is still missing, so this is not a configuration)
        done = lab.run(script, STICK_DEVICE_PORT=str(lab.device_port), STICK_PANEL_PORT=str(lab.panel_port))
        assert done.returncode != 0 and "advertise_host" in done.stderr and "host.example.conf" in done.stderr, (script.name, done.stdout, done.stderr)
        assert "10.0.0." not in done.stdout + done.stderr
    assert lab.servers() == [] and lab.opened.read_text() == "" and not lab.terminal_log.exists()


@with_lab()
def both_launchers_reject_an_invalid_configuration_without_starting_anything(lab):
    for text in ("[server]\nadvertise_host = not a host!\n", "[server\nbroken"):
        (lab.root / "config" / "host.conf").write_text(text)
        for script in (lab.start_copy, lab.panel_copy):
            done = lab.run(script)
            assert done.returncode != 0 and done.stderr.strip(), (text, script.name, done.stdout, done.stderr)
            assert "Traceback" not in done.stderr
    # Invalid PORTS in the file: the test ports the launchers must be given (environment > file) would mask them, so these are checked through the exact
    # configuration step both launchers run first (`stick_config.py shell`, with `--no-server` for the panel), with no port override; a failure there
    # stops either launcher, as the cases above show.
    env = {k: v for k, v in lab.env().items() if k not in ("STICK_DEVICE_PORT", "STICK_PANEL_PORT")}
    for text in (f"[server]\nadvertise_host = 192.0.2.10\ndevice_port = 99999\n", f"[server]\nadvertise_host = 192.0.2.10\ndevice_port = 5000\npanel_port = 5000\n"):
        (lab.root / "config" / "host.conf").write_text(text)
        for extra in ([], ["--no-server"]):
            done = subprocess.run([sys.executable, str(lab.root / "stick_config.py"), "shell", *extra], capture_output=True, text=True, env=env,
                                  stdin=subprocess.DEVNULL, timeout=60)
            assert done.returncode != 0 and done.stderr.strip() and "Traceback" not in done.stderr, (text, extra, done.stdout, done.stderr)
    assert lab.servers() == [] and lab.opened.read_text() == ""


@with_lab()
def a_missing_server_environment_is_explained_and_nothing_starts(lab):
    shutil.rmtree(lab.voice)
    done = lab.run(lab.start_copy)
    assert done.returncode == 1 and "voice" in done.stdout.lower() and lab.servers() == [], (done.stdout, done.stderr)


@with_lab()
def the_server_and_the_panel_bind_only_to_the_configured_loopback_addresses(lab):
    lab.spawn(lab.start_copy)
    assert wait_for(lambda: listening(lab.device_port) and listening(lab.panel_port))
    listeners = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", ",".join(map(str, lab.servers()))], capture_output=True, text=True).stdout
    for port in (lab.device_port, lab.panel_port):
        assert f"127.0.0.1:{port}" in listeners and f"*:{port}" not in listeners, listeners


if __name__ == "__main__":
    failures = 0
    for fn in CASES:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as error:                      # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(error).__name__}: {error}")
    print(f"{len(CASES) - failures}/{len(CASES)} launcher end-to-end cases passed")
    sys.exit(1 if failures else 0)
