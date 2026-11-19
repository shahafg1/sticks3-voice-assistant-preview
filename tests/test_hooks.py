"""Tests for hooks/stick_notify.py and hooks/manage_claude_hooks.py.

Run: python3 tests/test_hooks.py
The hook is run as a real subprocess, exactly as Claude Code runs it, against a
local HTTP server; the installer is run on temporary settings files.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "stick_notify.py"
MANAGE = ROOT / "hooks" / "manage_claude_hooks.py"
sys.path.insert(0, str(ROOT / "hooks"))
import manage_claude_hooks as manage  # noqa: E402


class Receiver:
    """Local HTTP server recording POST /notify bodies."""
    def __init__(self, behaviour="ok"):
        self.events = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if behaviour == "hang":
                    time.sleep(10)
                    return
                outer.events.append(json.loads(body))
                reply = b'{"queued": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/notify"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def assert_loopback(url):
    """The hook is run with `python -I -S`, so the audit guard cannot see inside it: make sure it can only talk to a local receiver."""
    assert url.startswith("http://127.0.0.1:") and ":8000" not in url and ":8001" not in url, f"hook test must not use a live endpoint: {url}"


def run_hook(mode, payload, url, env_extra=None, raw=None):
    assert_loopback(url)
    assert "STICK_NOTIFY_URL" not in (env_extra or {}) or (env_extra or {})["STICK_NOTIFY_URL"].startswith("http://127.0.0.1:")
    env = {k: v for k, v in os.environ.items() if not k.startswith("STICK_NOTIFY")}
    env["STICK_NOTIFY_URL"] = url
    env.update(env_extra or {})
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-I", "-S", str(HOOK), mode],
                          input=raw if raw is not None else json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=10)
    return proc, time.monotonic() - started


STOP = {"session_id": "abc123", "prompt_id": "p-1", "cwd": "/Users/me/projects/my-app",
        "hook_event_name": "Stop", "stop_hook_active": False,
        "last_assistant_message": "SECRET reply text that must never be sent"}
PERMISSION = {"session_id": "abc123", "cwd": "/Users/me/projects/my-app", "hook_event_name": "Notification",
              "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash",
              "title": "Permission needed"}

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def unused_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/notify"


@case
def stop_hook_sends_only_minimal_fields():
    r = Receiver()
    proc, _ = run_hook("finished", STOP, r.url)
    r.close()
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == "", (proc.returncode, proc.stdout, proc.stderr)
    assert r.events == [{"kind": "turn_finished", "label": "my-app", "session": "abc123", "prompt": "p-1"}], r.events
    assert "SECRET" not in json.dumps(r.events)


@case
def permission_notification_becomes_attention():
    r = Receiver()
    proc, _ = run_hook("attention", PERMISSION, r.url)
    r.close()
    assert proc.returncode == 0
    assert r.events == [{"kind": "attention", "label": "my-app", "session": "abc123", "prompt": ""}], r.events
    assert "permission" not in json.dumps(r.events).lower()


@case
def other_notification_types_are_ignored():
    r = Receiver()
    for kind in ("auth_success", "idle_prompt", "quota_auto_resume_fired", None):
        payload = dict(PERMISSION, notification_type=kind)
        proc, _ = run_hook("attention", payload, r.url)
        assert proc.returncode == 0
    r.close()
    assert r.events == []


@case
def voice_assistant_subprocess_is_excluded():
    r = Receiver()
    for mode, payload in (("finished", STOP), ("attention", PERMISSION)):
        proc, _ = run_hook(mode, payload, r.url, {"STICK_NOTIFY_SKIP": "1"})
        assert proc.returncode == 0
    r.close()
    assert r.events == []


@case
def server_down_exits_zero_quickly_and_silently():
    proc, took = run_hook("finished", STOP, unused_port_url())
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert took < 1.0, took


@case
def hanging_server_is_bounded():
    r = Receiver("hang")
    proc, took = run_hook("finished", STOP, r.url)
    r.close()
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert took < 2.5, took


@case
def garbage_input_never_fails():
    r = Receiver()
    for raw in ("", "not json", "[1,2,3]", "null", "\x00\xff"):
        proc, _ = run_hook("finished", None, r.url, raw=raw)
        assert proc.returncode == 0 and proc.stderr == "", (raw, proc.returncode, proc.stderr)
    r.close()
    # unparseable input still reports the turn, with a generic label
    assert all(e["label"] == "Claude" for e in r.events), r.events


@case
def unknown_mode_is_harmless():
    r = Receiver()
    proc, _ = run_hook("bogus", STOP, r.url)
    r.close()
    assert proc.returncode == 0 and r.events == []


@case
def debug_log_records_what_happened():
    r = Receiver()
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "hook.log"
        run_hook("finished", STOP, r.url, {"STICK_NOTIFY_DEBUG": str(log)})
        run_hook("finished", STOP, r.url, {"STICK_NOTIFY_DEBUG": str(log), "STICK_NOTIFY_SKIP": "1"})
        text = log.read_text()
    r.close()
    assert "sent" in text and "skipped (STICK_NOTIFY_SKIP)" in text, text


# ---- installer ---------------------------------------------------------------------------

SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "Stop": [{"hooks": [
            {"type": "command", "command": 'afplay "$HOME/Library/Sounds/x.mp3" 2>/dev/null || true', "async": True},
            {"type": "command", "command": "/home/u/traffic-light/set-state.sh idle", "async": True}]}],
        "Notification": [{"matcher": "permission_prompt", "hooks": [
            {"type": "command", "command": "/home/u/traffic-light/set-state.sh waiting", "async": True}]}],
        "PreToolUse": [{"hooks": [{"type": "command", "command": "/home/u/traffic-light/set-state.sh working"}]}],
    },
    "unicode": "שלום",
}


def with_settings(settings, mode=0o600):
    d = tempfile.TemporaryDirectory()
    path = Path(d.name) / "settings.json"
    path.write_text(manage.dump(settings))
    os.chmod(path, mode)
    return d, path


def run_manage(action, path):
    return subprocess.run([sys.executable, str(MANAGE), action, "--settings", str(path), "--python", sys.executable],
                          capture_output=True, text=True)


@case
def install_adds_hooks_and_keeps_everything_else():
    d, path = with_settings(SETTINGS)
    proc = run_manage("install", path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    new = json.loads(path.read_text())
    # existing hooks and unrelated keys are untouched
    for key in ("model", "permissions", "unicode"):
        assert new[key] == SETTINGS[key], key
    assert new["hooks"]["PreToolUse"] == SETTINGS["hooks"]["PreToolUse"]
    assert new["hooks"]["Stop"][0] == SETTINGS["hooks"]["Stop"][0]
    assert new["hooks"]["Notification"][0] == SETTINGS["hooks"]["Notification"][0]
    # exactly one group appended to each of the two events
    assert len(new["hooks"]["Stop"]) == 2 and len(new["hooks"]["Notification"]) == 2
    stop_cmd = new["hooks"]["Stop"][1]["hooks"][0]["command"]
    note = new["hooks"]["Notification"][1]
    assert "stick_notify.py finished || true" in stop_cmd and "-I -S" in stop_cmd
    assert note["matcher"] == manage.ATTENTION_MATCHER and "stick_notify.py attention" in note["hooks"][0]["command"]
    assert note["hooks"][0]["timeout"] == 3 and "async" not in note["hooks"][0]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert len(list(path.parent.glob("settings.json.bak-stick-notify-*"))) == 1
    d.cleanup()


@case
def install_is_idempotent():
    d, path = with_settings(SETTINGS)
    run_manage("install", path)
    after_first = path.read_text()
    proc = run_manage("install", path)
    assert proc.returncode == 0 and "Nothing to do" in proc.stdout, proc.stdout
    assert path.read_text() == after_first
    assert len(list(path.parent.glob("settings.json.bak-stick-notify-*"))) == 1   # no second backup
    d.cleanup()


@case
def uninstall_restores_the_original_exactly():
    d, path = with_settings(SETTINGS)
    original = path.read_text()
    run_manage("install", path)
    assert path.read_text() != original
    proc = run_manage("uninstall", path)
    assert proc.returncode == 0, proc.stdout
    assert path.read_text() == original
    d.cleanup()


@case
def uninstall_keeps_settings_changed_after_install():
    d, path = with_settings(SETTINGS)
    run_manage("install", path)
    changed = json.loads(path.read_text())
    changed["model"] = "sonnet"                              # the user edits something later
    changed["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": "/home/u/new.sh"}]})
    path.write_text(manage.dump(changed))
    run_manage("uninstall", path)
    final = json.loads(path.read_text())
    assert final["model"] == "sonnet" and final["hooks"]["PreToolUse"][-1]["hooks"][0]["command"] == "/home/u/new.sh"
    assert not any(manage.is_ours(h) for g in final["hooks"]["Stop"] for h in g["hooks"])
    assert len(final["hooks"]["Stop"]) == 1 and len(final["hooks"]["Notification"]) == 1
    d.cleanup()


@case
def install_into_settings_without_hooks_and_back():
    plain = {"model": "opus"}
    d, path = with_settings(plain)
    run_manage("install", path)
    assert set(json.loads(path.read_text())["hooks"]) == {"Stop", "Notification"}
    run_manage("uninstall", path)
    assert json.loads(path.read_text()) == plain
    d.cleanup()


@case
def unreadable_settings_are_never_modified():
    d = tempfile.TemporaryDirectory()
    path = Path(d.name) / "settings.json"
    path.write_text("{ this is not json")
    proc = run_manage("install", path)
    assert proc.returncode == 1 and "Nothing changed" in proc.stdout
    assert path.read_text() == "{ this is not json"
    assert not list(path.parent.glob("*.bak-*"))
    d.cleanup()


@case
def status_reports_installed_state():
    d, path = with_settings(SETTINGS)
    assert "not installed" in run_manage("status", path).stdout
    run_manage("install", path)
    out = run_manage("status", path).stdout
    assert "installed (2)" in out and "backup:" in out
    d.cleanup()


@case
def uninstall_when_not_installed_does_nothing():
    d, path = with_settings(SETTINGS)
    before = path.read_text()
    proc = run_manage("uninstall", path)
    assert "Nothing to do" in proc.stdout and path.read_text() == before
    assert not list(path.parent.glob("*.bak-*"))
    d.cleanup()


@case
def installed_command_really_runs_the_hook():
    # take the exact command string the installer writes and run it through a shell
    d, path = with_settings(SETTINGS)
    run_manage("install", path)
    cmd = json.loads(path.read_text())["hooks"]["Stop"][1]["hooks"][0]["command"]
    r = Receiver()
    env = {k: v for k, v in os.environ.items() if not k.startswith("STICK_NOTIFY")}
    env["STICK_NOTIFY_URL"] = r.url
    assert_loopback(r.url)
    proc = subprocess.run(cmd, shell=True, input=json.dumps(STOP), capture_output=True, text=True, env=env, timeout=10)
    r.close()
    assert proc.returncode == 0 and len(r.events) == 1 and r.events[0]["label"] == "my-app", (proc, r.events)
    d.cleanup()


@case
def missing_interpreter_still_exits_zero():
    cmd = manage.command_for("finished", "/nonexistent/python")
    proc = subprocess.run(cmd, shell=True, input="{}", capture_output=True, text=True, timeout=10,
                          env={**{k: v for k, v in os.environ.items() if not k.startswith("STICK_NOTIFY")},
                               "STICK_NOTIFY_URL": "http://127.0.0.1:9/notify"})      # (the interpreter does not exist, so nothing is sent anyway)
    assert proc.returncode == 0


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:300])
        except Exception as error:
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:300])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
