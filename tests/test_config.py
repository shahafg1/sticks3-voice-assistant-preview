"""Tests for the host configuration (stick_config.py), the Stick's own configuration (stick/server_config.py), and their use by the launchers, the hook and
the deploy tool. Run: python3 tests/test_config.py

Everything runs in temporary folders (some with spaces and brackets in their names) and on ephemeral ports; the deploy tool is exercised against a SIMULATED
the Stick tool and the Stick against simulated stand-ins. Nothing here touches the live server, data, secrets, the Stick or the network.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import ast
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stick"))
import stick_config as sc  # noqa: E402
import server_config as dev_cfg  # noqa: E402

CASES = []
FIXTURES = ROOT / "tests" / "fixtures"
PRIVATE_IPV4 = re.compile(r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")   # a LAN address baked into code
PORT = sc.DEFAULT_DEVICE_PORT


def case(fn):
    CASES.append(fn)
    return fn


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def project(tmp, name="My Stick (copy) v2.1", files=()):
    """A copy of the parts of this project that read the configuration, in a folder whose name has spaces and brackets."""
    root = Path(tmp) / name
    for rel in ("stick_config.py", "config/host.example.conf", "tools/deploy_stick.sh", "tools/stick_setup.py", "tools/stick_backup.py", "tools/stick_restore.py", "tools/stick_install.py",
                "Start Voice Assistant.command", "Voice Assistant Panel.command", "hooks/stick_notify.py", "tools/preflight.py",
                "requirements/server.txt", "requirements/tts.txt", "requirements/stick-tools.txt", "requirements/models.json",
                "stick/server_config.py", "stick/notify.py", "stick/device.py", "stick/sleep.py", "stick/reminder.py", "stick/robot.py", "stick/voice_send.py",
                "stick/mic.py", "stick/statick_shapes.py", "stick/shake.py", "stick/main.py", *files):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    return root


def write_conf(root, text):
    path = root / "config" / "host.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def run_cli(root, *args, env=None, cwd=None):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("STICK_") and k != "MPREMOTE"}
    environment.update(env or {})
    return subprocess.run([sys.executable, str(root / "stick_config.py"), *args], capture_output=True, text=True, cwd=cwd or tempfile.gettempdir(), env=environment, timeout=60)


def shell_values(cfg):
    """The launchers' CFG_* variables as the shell would see them (quotes removed)."""
    return {k: (sc.shlex.split(v) or [""])[0] for k, v in (line.split("=", 1) for line in sc.shell_lines(cfg).splitlines())}


def raises_config(fn, *needles):
    try:
        fn()
    except sc.ConfigError as error:
        message = str(error)
        for needle in needles:
            assert needle in message, f"{needle!r} missing from: {message}"
        return message
    raise AssertionError("expected ConfigError")


# ---- missing and invalid configuration ------------------------------------------------------------------------------

@case
def a_new_installation_without_configuration_gets_an_actionable_message_and_no_default_address():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        message = raises_config(lambda: sc.load(root, env={}), "advertise_host", "host.example.conf", "init --advertise-host", "suggest-address")
        assert not PRIVATE_IPV4.search(message)                                # no built-in LAN address is ever offered as a fallback
        done = run_cli(root, "validate")
        assert done.returncode == 2 and "advertise_host" in done.stderr and not done.stdout
        assert run_cli(root, "shell").returncode == 2 and run_cli(root, "device-config").returncode == 2
        assert not (root / "config" / "host.conf").exists() and not (root / "build").exists()     # asking never creates anything



@case
def the_runtime_files_contain_no_built_in_lan_address():
    offenders = []
    for rel in ("audio_server.py", "tts.py", "panel_server.py", "device_link.py", "stick_config.py", "hooks/stick_notify.py", "hooks/manage_claude_hooks.py",
                "stick/voice_send.py", "stick/notify.py", "stick/device.py", "stick/server_config.py", "tools/deploy_stick.sh", "tools/preflight.py",
                "Start Voice Assistant.command", "Voice Assistant Panel.command"):
        offenders += [f"{rel}: {m.group(0)}" for m in PRIVATE_IPV4.finditer((ROOT / rel).read_text())]
    assert not offenders, f"hard-coded LAN addresses remain in: {offenders}"


@case
def invalid_values_are_all_reported_together_with_what_to_do():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        bad = {
            "advertise_host = 127.0.0.1": "cannot be used",
            "advertise_host = 0.0.0.0": "cannot be used",
            "advertise_host = localhost": "neither an IPv4 address nor",
            "advertise_host = not a host!": "neither an IPv4 address nor",
            "advertise_host = mac.local": "bind_host must be set",
            "advertise_host = 192.0.2.10\nbind_host = somewhere": "bind_host",
            "advertise_host = 192.0.2.10\ndevice_port = abc": "device_port must be a whole number",
            "advertise_host = 192.0.2.10\ndevice_port = 80": "between 1024 and 65535",
            "advertise_host = 192.0.2.10\ndevice_port = 9000\npanel_port = 9000": "must differ",
        }
        for text, needle in bad.items():
            write_conf(root, "[server]\n" + text + "\n")
            raises_config(lambda: sc.load(root, env={}), needle, str(root / "config" / "host.conf"))
        write_conf(root, "[server]\nadvertise_host = 192.0.2.10\n[device]\nusb_port = COM3\n")
        raises_config(lambda: sc.load(root, env={}), "usb_port")
        write_conf(root, "[server]\nadvertize_host = 192.0.2.10\n")             # a typo is an error, not silently ignored
        raises_config(lambda: sc.load(root, env={}), "unknown setting 'advertize_host'")
        write_conf(root, "[servr]\nadvertise_host = 192.0.2.10\n")
        raises_config(lambda: sc.load(root, env={}), "unknown section [servr]")
        write_conf(root, "[server]\nadvertise_host = 127.0.0.1\ndevice_port = 80\n")
        message = raises_config(lambda: sc.load(root, env={}), "cannot be used", "between 1024 and 65535")   # both problems in one message
        assert "Edit " in message
        raises_config(lambda: sc.load(root, env={"STICK_CONFIG": str(Path(tmp) / "nope.conf")}), "does not exist")


@case
def a_host_name_needs_an_explicit_listen_address_and_the_listen_address_never_widens_silently():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        write_conf(root, "[server]\nadvertise_host = mac.local\nbind_host = 0.0.0.0\n")
        cfg = sc.load(root, env={})
        assert cfg.advertise_host == "mac.local" and cfg.bind_host == "0.0.0.0" and cfg.connect_host == "127.0.0.1"
        write_conf(root, "[server]\nadvertise_host = 192.0.2.10\n")
        cfg = sc.load(root, env={})
        assert cfg.bind_host == "192.0.2.10" and cfg.sources["bind_host"] == "derived"     # listens on the advertised address only, as before
        assert cfg.notify_url == f"http://127.0.0.1:{cfg.panel_port}/hook/notify"      # the hook always uses the loopback-only panel listener


# ---- derivation, defaults, precedence -----------------------------------------------------------------------------

@case
def every_location_is_derived_from_where_the_project_is_installed_even_with_spaces():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        write_conf(root, "[server]\nadvertise_host = 192.0.2.10\n")
        elsewhere = Path(tmp) / "some other working folder"
        elsewhere.mkdir()
        done = run_cli(root, "show", "--json", cwd=elsewhere)                    # the current folder must not matter
        data = json.loads(done.stdout)
        resolved = root.resolve()
        assert data["project_root"] == str(resolved) and " " in data["project_root"] and "(" in data["project_root"]
        for key, folder in (("voice_env", "voice-env"), ("tts_env", "tts-env312"), ("models_dir", "tts-models"), ("data_dir", "data"),
                            ("secrets_dir", "secrets"), ("recordings_dir", "recordings"), ("tools_env", "tools-env"), ("whisper_dir", "whisper-model")):
            assert data[key] == str(resolved / folder), (key, data[key])
        shell = shell_values(sc.load(root, env={}))
        assert shell["CFG_MPREMOTE"] == str(root / "tools-env" / "bin" / sc.TOOL_PROGRAM)       # (in-process load: the folder as given)
        assert data["device_port"] == str(PORT) and data["panel_port"] == str(sc.DEFAULT_PANEL_PORT) and data["sources"]["device_port"] == "default"


@case
def environment_beats_file_and_file_beats_default_and_relative_and_tilde_paths_work():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        write_conf(root, "[server]\nadvertise_host = 192.0.2.10\ndevice_port = 9100\n[paths]\nmodels_dir = my models\nclaude = ~/bin/claude\nffmpeg = ffmpeg-custom\n")
        cfg = sc.load(root, env={"STICK_DEVICE_PORT": "9200", "STICK_PANEL_PORT": "9201", "STICK_RECORDINGS_DIR": "/somewhere/rec"})
        assert cfg.device_port == 9200 and cfg.sources["device_port"] == "env"
        assert cfg.panel_port == 9201 and cfg.recordings_dir == Path("/somewhere/rec")
        assert cfg.models_dir == root / "my models" and cfg.sources["models_dir"] == "file"
        shell = shell_values(cfg)
        assert shell["CFG_CLAUDE"].startswith("/") and "~" not in shell["CFG_CLAUDE"] and shell["CFG_CLAUDE"].endswith("/bin/claude")
        assert shell["CFG_FFMPEG"] == "ffmpeg-custom"                                   # a bare name stays a PATH lookup
        cfg = sc.load(root, env={"STICK_DEVICE_PORT": "  "})                       # an empty variable is "not set"
        assert cfg.device_port == 9100


@case
def alternate_ports_and_listen_address_are_honoured_and_the_panel_stays_a_separate_loopback_port():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        write_conf(root, "[server]\nadvertise_host = 192.0.2.10\nbind_host = 0.0.0.0\ndevice_port = 18000\npanel_port = 18001\n")
        cfg = sc.load(root, env={})
        assert (cfg.device_port, cfg.panel_port, cfg.bind_host) == (18000, 18001, "0.0.0.0")
        assert cfg.notify_url == "http://127.0.0.1:18001/hook/notify"            # loopback panel listener, never the LAN device port
        text = (ROOT / "panel_server.py").read_text()
        assert 'HOST = "127.0.0.1"' in text and "ThreadingHTTPServer((HOST, port)" in text    # the panel's address is not configurable


@case
def tools_that_only_need_locations_do_not_require_a_server_address():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        cfg = sc.load(root, env={}, require_server=False)
        assert cfg.advertise_host is None and str(cfg.tools_env).endswith("tools-env")
        done = run_cli(root, "shell", "--no-server")
        assert done.returncode == 0 and f"CFG_DEVICE_PORT={PORT}" in done.stdout


@case
def shell_output_survives_spaces_quotes_and_brackets_when_evaluated_by_the_launchers_shell():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp, name="it's a (test) folder & more")
        write_conf(root, "[server]\nadvertise_host = 192.0.2.10\n[paths]\nclaude = my claude\n")
        code = f'eval "$({sys.executable} {sc.shlex.quote(str(root / "stick_config.py"))} shell)"; printf "%s|%s|%s" "$CFG_PROJECT" "$CFG_CLAUDE" "$CFG_DEVICE_PORT"'
        done = subprocess.run(["zsh", "-c", code], capture_output=True, text=True, env={k: v for k, v in os.environ.items() if not k.startswith("STICK_")})
        assert done.stdout == f"{root.resolve()}|my claude|{PORT}", done


@case
def usb_selection_prefers_the_setting_then_the_only_device_and_never_guesses_among_several():
    with tempfile.TemporaryDirectory() as tmp:
        dev = Path(tmp) / "dev"
        dev.mkdir()
        root = project(tmp)
        write_conf(root, "[server]\nadvertise_host = 192.0.2.10\n")
        cfg = sc.load(root, env={})
        assert sc.resolve_usb_port(cfg, str(dev))[0] == "" and "no USB" in sc.resolve_usb_port(cfg, str(dev))[1]
        (dev / f"{sc.USB_PREFIX}1234").write_text("")
        port, note = sc.resolve_usb_port(cfg, str(dev))
        assert port == f"{dev}/{sc.USB_PREFIX}1234" and note.startswith("auto-selected")
        (dev / f"{sc.USB_PREFIX}5678").write_text("")
        port, note = sc.resolve_usb_port(cfg, str(dev))
        assert port == "" and "several" in note and "usb_port" in note
        chosen = f"/dev/{sc.USB_PREFIX}ABC"
        write_conf(root, f"[server]\nadvertise_host = 192.0.2.10\n[device]\nusb_port = {chosen}\n")
        assert sc.resolve_usb_port(sc.load(root, env={}), str(dev))[0] == chosen
        other = f"/dev/{sc.USB_PREFIX}XYZ"
        assert sc.load(root, env={"STICK_PORT": other}).usb_port == other      # the earlier STICK_PORT variable still works


# ---- upgrade path ------------------------------------------------------------------------------------------------------

@case
def an_existing_installation_upgrades_with_one_explicit_command_that_never_overwrites_or_guesses():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        home = Path(tmp) / "home"
        (home / "sticks3-env" / "bin").mkdir(parents=True)
        (home / "sticks3-env" / "bin" / sc.TOOL_PROGRAM).write_text("")
        bad = run_cli(root, "init", "--advertise-host", "127.0.0.1")
        assert bad.returncode == 2 and not (root / "config" / "host.conf").exists()          # validated before anything is written
        target = sc.init_file(root, "192.0.2.10", keep_existing_locations=True, home=home)
        text = target.read_text()
        assert "advertise_host = 192.0.2.10" in text and f"tools_env = {home / 'sticks3-env'}" in text
        cfg = sc.load(root, env={})
        assert cfg.tools_env == home / "sticks3-env" and cfg.advertise_host == "192.0.2.10"
        before = target.read_text()
        again = run_cli(root, "init", "--advertise-host", "192.0.2.99")
        assert again.returncode == 2 and "already exists" in again.stderr and target.read_text() == before


@case
def init_can_prepare_another_project_folder_before_it_is_upgraded_and_the_old_code_there_ignores_the_file():
    with tempfile.TemporaryDirectory() as tmp:
        here = project(tmp, name="tool copy")
        live = Path(tmp) / "Installed (old code)"
        live.mkdir()
        done = run_cli(here, "init", "--advertise-host", "192.0.2.10", "--project", str(live))
        assert done.returncode == 0, done.stderr
        assert (live / "config" / "host.conf").is_file() and not (here / "config" / "host.conf").exists()
        assert sc.load(live, env={}).advertise_host == "192.0.2.10"
        again = run_cli(here, "init", "--advertise-host", "192.0.2.99", "--project", str(live))
        assert again.returncode == 2 and "already exists" in again.stderr


# ---- the device's own configuration -------------------------------------------------------------------------------------

@case
def the_generated_device_configuration_holds_only_the_server_address_and_port():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        write_conf(root, f"[server]\nadvertise_host = 192.0.2.10\ndevice_port = 18000\n[device]\nusb_port = /dev/{sc.USB_PREFIX}ABC\n[paths]\nclaude = /opt/x/claude\ndata_dir = /srv/private-data\n")
        out = root / "build" / "device_config.json"
        done = run_cli(root, "device-config", "--out", str(out))
        assert done.returncode == 0
        data = json.loads(out.read_text())
        assert data == {"v": 1, "server_host": "192.0.2.10", "server_port": 18000}
        raw = out.read_text()
        for private in ("/opt/x", "/srv", "private-data", sc.USB_PREFIX, "claude", "secret", "pswd", "ssid", "password", "device_key"):
            assert private not in raw, private
        assert dev_cfg.parse(raw) == ("192.0.2.10", 18000)                          # and the Stick's own parser accepts exactly this


@case
def the_stick_refuses_missing_or_unusable_server_settings_instead_of_guessing():
    good = json.dumps({"v": 1, "server_host": "192.0.2.10", "server_port": 8000})
    assert dev_cfg.load(lambda path: good) == ("192.0.2.10", 8000, None)
    assert dev_cfg.load(lambda path: None)[2].startswith("missing /flash/device_config.json")
    for bad in ("", "{", "[]", "{}", json.dumps({"v": 2, "server_host": "192.0.2.10", "server_port": 8000}),
                json.dumps({"v": True, "server_host": "192.0.2.10", "server_port": 8000}),
                json.dumps({"v": 1, "server_host": "127.0.0.1", "server_port": 8000}),
                json.dumps({"v": 1, "server_host": "0.0.0.0", "server_port": 8000}),
                json.dumps({"v": 1, "server_host": "localhost", "server_port": 8000}),
                json.dumps({"v": 1, "server_host": "bad host", "server_port": 8000}),
                json.dumps({"v": 1, "server_host": "", "server_port": 8000}),
                json.dumps({"v": 1, "server_host": "192.0.2.10", "server_port": 80}),
                json.dumps({"v": 1, "server_host": "192.0.2.10", "server_port": "8000"}),
                json.dumps({"v": 1, "server_host": "192.0.2.10", "server_port": True}),
                json.dumps({"v": 1, "server_host": "192.0.2.10"})):
        host, port, error = dev_cfg.load(lambda path, t=bad: t)
        assert host is None and port is None and error, bad
    assert not hasattr(sys.modules["notify"] if "notify" in sys.modules else __import__("notify"), "HOST")      # no built-in server on the Stick
    stick_source = (ROOT / "stick" / "voice_send.py").read_text()
    assert "No server" in stick_source and "server_config.load()" in stick_source


class Recorder(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        Recorder.seen.append((self.command, self.path, self.headers.get("X-Device-Key"), self.headers.get("Host")))
        body = json.dumps({"store": "S", "rev": 1, "settings": {}, "commands": [], "notification": None, "reminder": None, "poll_ms": 1000}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@case
def a_simulated_stick_syncs_with_the_address_and_port_taken_from_the_generated_configuration():
    own = isolation_guard._OWN_LAN
    if not own:
        return                                                                       # no LAN address on this machine: nothing to connect through
    import device
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Recorder)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Recorder.seen.clear()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = project(tmp)
            write_conf(root, f"[server]\nadvertise_host = {own}\nbind_host = 0.0.0.0\ndevice_port = {port}\n")
            out = root / "build" / "device_config.json"
            assert run_cli(root, "device-config", "--out", str(out)).returncode == 0
            host, dport, error = dev_cfg.load(lambda path: out.read_text())
            assert error is None and (host, dport) == (own, port)

            class Lcd:
                def getBrightness(self):
                    return 128

                def setBrightness(self, raw):
                    pass

            files = {}
            state = device.DeviceState(Lcd(), None, None, "fw", read=lambda p: files.get(p), write=lambda p, t: files.__setitem__(p, t), remove=lambda p: files.pop(p, None))
            poller = device.DeviceSync(state, host, dport, interval_ms=50, key="k" * 40)
            deadline = time.time() + 8
            while not Recorder.seen and time.time() < deadline:
                poller.step()
                time.sleep(0.02)
        assert Recorder.seen and Recorder.seen[0][:3] == ("POST", "/device/sync", "k" * 40), Recorder.seen
        assert Recorder.seen[0][3] == own                                                  # the Host header proves which configured address was used
    finally:
        server.shutdown()


@case
def a_device_without_any_server_address_sends_nothing():
    import device

    class Lcd:
        def getBrightness(self):
            return 128

        def setBrightness(self, raw):
            pass

    files = {}
    state = device.DeviceState(Lcd(), None, None, "fw", read=lambda p: files.get(p), write=lambda p, t: files.__setitem__(p, t), remove=lambda p: files.pop(p, None))
    poller = device.DeviceSync(state, key="k" * 40)
    for _ in range(50):
        assert poller.step() is None
    assert poller.sock is None and poller.failures >= 1                               # it only backs off: no socket was ever opened


# ---- the hook ---------------------------------------------------------------------------------------------------------------

class Collector(http.server.BaseHTTPRequestHandler):
    got = []
    headers = []

    def do_POST(self):
        Collector.got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        Collector.headers.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def run_hook(root, env):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("STICK_")}
    environment.update(env)
    payload = json.dumps({"session_id": "s", "prompt_id": "p", "cwd": "/work/project"})
    return subprocess.run([sys.executable, "-I", "-S", str(root / "hooks" / "stick_notify.py"), "finished"], input=payload, capture_output=True, text=True,
                          env=environment, timeout=15)


@case
def the_hook_finds_the_server_through_the_configuration_and_stays_silent_without_one():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        Collector.got.clear()
        Collector.headers.clear()
        try:
            log = Path(tmp) / "hook.log"
            write_conf(root, f"[server]\nadvertise_host = 192.0.2.10\nbind_host = 127.0.0.1\npanel_port = {port}\n")
            assert run_hook(root, {"STICK_NOTIFY_DEBUG": str(log)}).returncode == 0
            assert Collector.got and Collector.got[0][0] == "/hook/notify" and Collector.got[0][1]["kind"] == "turn_finished"
            assert Collector.headers[0].get("X-Stick-Hook") == "1" and "Origin" not in Collector.headers[0]
            Collector.got.clear()
            assert run_hook(root, {"STICK_NOTIFY_URL": f"http://127.0.0.1:{port}/custom"}).returncode == 0      # the explicit variable still wins
            assert Collector.got and Collector.got[0][0] == "/custom"
            Collector.got.clear()
            (root / "config" / "host.conf").unlink()                                   # no configuration, no variable: never guess an address
            done = run_hook(root, {"STICK_NOTIFY_DEBUG": str(log)})
            assert done.returncode == 0 and not Collector.got
            assert "not sent" in log.read_text()
            write_conf(root, "[server]\nadvertise_host = 127.0.0.1\n")                # invalid configuration: also silent, also exit 0
            assert run_hook(root, {}).returncode == 0 and not Collector.got
        finally:
            server.shutdown()


# ---- launchers and tools ---------------------------------------------------------------------------------------------------

def fake_tools(tmp, mark_claude=False):
    bindir = Path(tmp) / "fake bin"
    bindir.mkdir(exist_ok=True)
    marker = Path(tmp) / "claude-was-run"
    for name, body in (("claude", f'#!/bin/sh\ntouch "{marker}"\n' if mark_claude else "#!/bin/sh\nexit 0\n"), ("ffmpeg", "#!/bin/sh\nexit 0\n")):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    return bindir, marker


def run_launcher(root, tmp, extra_path, env=None):
    home = Path(tmp) / "home"
    home.mkdir(exist_ok=True)
    system = "/usr/bin:/bin:/usr/sbin:/sbin"
    python_dir = str(Path(shutil.which("python3")).parent)
    environment = {"HOME": str(home), "PATH": f"{extra_path}:{python_dir}:{system}"}
    # Explicit, distinct, non-live ports (the isolation guard refuses a port-aware script without them; a launcher left to its defaults would
    # probe the running assistant). The device port is the one the test wrote into its configuration, so a busy-port check still tests it.
    conf = root / "config" / "host.conf"
    configured = re.search(r"^device_port\s*=\s*(\d+)", conf.read_text(), re.M) if conf.exists() else None
    device = int(configured.group(1)) if configured else free_port()
    panel = free_port()
    while panel == device:
        panel = free_port()
    environment.update({"STICK_DEVICE_PORT": str(device), "STICK_PANEL_PORT": str(panel)})
    environment.update(env or {})
    # run an identical COPY under another name: the isolation guard refuses to start anything called like the real launcher (it must never be started by a test)
    copy = root / "launcher-under-test.command"
    shutil.copy2(root / "Start Voice Assistant.command", copy)
    return subprocess.run(["zsh", str(copy)], capture_output=True, text=True, env=environment, stdin=subprocess.DEVNULL, timeout=60)


def fake_server_python(root, tmp):
    log = Path(tmp) / "server-start.log"
    python = root / "voice-env" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\necho "$@" > "{log}"\nexit 0\n')
    python.chmod(0o755)
    (root / "audio_server.py").write_text("# stand-in\n")
    return log


@case
def the_launcher_works_from_any_folder_and_reads_the_configuration_without_starting_the_real_server():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        bindir, _ = fake_tools(tmp)
        log = fake_server_python(root, tmp)
        port = free_port()
        write_conf(root, f"[server]\nadvertise_host = 192.0.2.10\nbind_host = 127.0.0.1\ndevice_port = {port}\n")
        done = run_launcher(root, tmp, str(bindir))
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert log.read_text().strip() == str((root / "audio_server.py").resolve()) or log.read_text().strip() == str(root / "audio_server.py"), log.read_text()


@case
def the_launcher_explains_a_missing_configuration_a_busy_port_and_a_missing_tool_and_starts_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        bindir, _ = fake_tools(tmp)
        log = fake_server_python(root, tmp)
        done = run_launcher(root, tmp, str(bindir))                                      # no configuration at all
        assert "advertise_host" in done.stderr and "host.example.conf" in done.stderr and not log.exists()
        port = free_port()
        write_conf(root, f"[server]\nadvertise_host = 192.0.2.10\nbind_host = 127.0.0.1\ndevice_port = {port}\n")
        listener = socket.socket()
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        try:
            done = run_launcher(root, tmp, str(bindir))
            assert str(port) in done.stdout and "פורט" in done.stdout and not log.exists()
        finally:
            listener.close()
        done = run_launcher(root, tmp, str(Path(tmp) / "no-tools"))                       # claude and ffmpeg not on PATH
        assert "claude" in done.stdout and not log.exists()
        (root / "voice-env" / "bin" / "python").unlink()
        done = run_launcher(root, tmp, str(bindir))
        assert "voice-env" in done.stdout and not log.exists()


@case
def the_panel_launcher_and_every_script_parse_and_use_the_configuration():
    with tempfile.TemporaryDirectory() as tmp:
        for index, name in enumerate(("Start Voice Assistant.command", "Voice Assistant Panel.command", "tools/deploy_stick.sh")):
            neutral = Path(tmp) / f"script{index}.sh"                                      # a syntax check of a COPY: the guard refuses processes named like the real launchers
            shutil.copy2(ROOT / name, neutral)
            assert subprocess.run(["zsh", "-n", str(neutral)], capture_output=True).returncode == 0, name
            assert "stick_config.py" in (ROOT / name).read_text(), name
    panel = (ROOT / "Voice Assistant Panel.command").read_text()
    assert "--no-server" in panel and "${0:A:h}" in panel and str(sc.DEFAULT_PANEL_PORT) not in panel.replace("# ", "")


@case
def the_deploy_tool_stops_before_touching_the_stick_without_configuration_and_otherwise_sends_the_device_configuration_with_the_code():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        store, log = Path(tmp) / "store", Path(tmp) / "calls.log"
        store.mkdir()
        fake = Path(tmp) / "fake-tool"
        shutil.copy2(FIXTURES / "fake_stick_tool.sh", fake)
        fake.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("STICK_") and k != "MPREMOTE"}
        env.update({"FAKE_TOOL_LOG": str(log), "FAKE_TOOL_STORE": str(store)})
        script = str(root / "tools" / "deploy_stick.sh")
        first = subprocess.run(["zsh", script], capture_output=True, text=True, env=env, timeout=60)
        assert first.returncode == 2 and "advertise_host" in first.stderr and not log.exists()          # no configuration: the Stick is not touched
        write_conf(root, (FIXTURES / "deploy_config.template").read_text().replace("@TOOL@", str(fake)))
        done = subprocess.run(["zsh", script], capture_output=True, text=True, env=env, timeout=60)
        assert done.returncode == 0 and "hashes verified" in done.stdout, (done.stdout, done.stderr)
        sent = {p.name for p in store.iterdir()}
        assert {"server_config.py", "device_config.json", "voice_send.py", "device.py", "notify.py"} <= sent
        uploaded = json.loads((store / "device_config.json").read_text())
        assert uploaded == {"v": 1, "server_host": "192.0.2.10", "server_port": PORT}                   # only what the device needs
        calls = log.read_text()
        assert calls.count("exec") == 1 and "machine.reset" in calls                                     # reset only after every hash matched
        copies = [line for line in calls.splitlines() if line.startswith("fs --no-verbose cp")]
        order = [line.rsplit("/", 1)[-1] for line in copies]
        # not atomic, and the order is risk reduction only (no intermediate mix is ever started): modules older Sticks lack before their
        # importers, notify.py (which dropped HOST/PORT that the OLD device.py imports) after device.py, main.py last
        assert order == ["device_config.json", "server_config.py", "mic.py", "statick_shapes.py", "shake.py", "voice_send.py", "device.py",
                         "sleep.py", "reminder.py", "robot.py", "notify.py", "main.py"], order


def stick_imports(path):
    """The Stick's own modules that a Stick source file imports (by parsing it; nothing is executed)."""
    local = {p.stem for p in (ROOT / "stick").glob("*.py")}
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found & local


@case
def the_deploy_list_covers_every_module_the_app_imports_in_a_safe_order():
    text = (ROOT / "tools" / "deploy_stick.sh").read_text()
    files = text.split("\nFILES=(")[1].split(")")[0].split()
    assert len(files) == len(set(files)), files
    needed, todo = set(), ["voice_send"]                       # main.py runs voice_send.py; follow the imports from there
    while todo:
        name = todo.pop()
        if name in needed:
            continue
        needed.add(name)
        todo += sorted(stick_imports(ROOT / "stick" / (name + ".py")))
    assert stick_imports(ROOT / "stick" / "main.py") == set()   # main.py execs voice_send.py by path, imports nothing local
    assert {n + ".py" for n in needed} | {"main.py"} == set(files), (sorted(needed), files)
    # modules that a v0.10.0 or v0.11.0 Stick does not have must be on the device before any file that imports them
    for new, importers in (("server_config.py", ("voice_send.py",)), ("mic.py", ("voice_send.py",)), ("statick_shapes.py", ("robot.py",))):
        for imp in importers:
            assert files.index(new) < files.index(imp), (new, imp, files)
    assert files.index("device.py") < files.index("notify.py") and files[-1] == "main.py"


@case
def the_deploy_tool_without_a_usb_device_stops_with_the_reason_and_the_setup_tool_works_from_any_folder():
    with tempfile.TemporaryDirectory() as tmp:
        root = project(tmp)
        write_conf(root, "[server]\nadvertise_host = 192.0.2.10\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("STICK_")}
        env["STICK_PORT"] = ""
        script = str(root / "tools" / "deploy_stick.sh")
        done = subprocess.run(["zsh", script], capture_output=True, text=True, env=env, timeout=60, cwd=tmp)
        # the result depends on how many USB devices this machine has attached; it must be a clear stop or a refusal, never a guess
        assert done.returncode != 0 or "hashes verified" not in done.stdout
        plan = subprocess.run([sys.executable, str(root / "tools" / "stick_setup.py"), "plan"], capture_output=True, text=True, env=env, cwd=tmp, timeout=60)
        assert plan.returncode == 0 and str(root / "secrets" / "device_key") in plan.stdout and "none yet" in plan.stdout, plan.stdout + plan.stderr


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
            print("ERROR", fn.__name__, repr(error)[:400])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
