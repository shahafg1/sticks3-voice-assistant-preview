"""Integration tests for the desktop panel in the real audio_server.py.

Run with the server's own interpreter:  voice-env/bin/python tests/test_panel_server.py

The real server runs as a subprocess on ephemeral ports. Only external tools are
stand-ins: a `claude` script that logs any call (the panel must never call AI) and a
stub mlx_whisper module (imported by the server but unused here).
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import http.client
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = "t" * 40
AUTH = {"X-Device-Key": KEY}
HOOK = {"Content-Type": "application/json", "X-Stick-Hook": "1"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def lan_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))                  # no packet is sent; this only selects a route
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") else ip


class Harness:
    def __init__(self, panel=True, key=KEY, db_bytes=None, db_version=None, busy_panel_port=False,
                 env_extra=None, seed_v070=False, seed_v080=False, fake_tts=False):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir_path = Path(self.dir.name)
        for sub in ("recordings", "bin", "stubs", "data", "secrets"):
            (d / sub).mkdir()
        py = sys.executable
        self.claude_log = d / "claude_calls.log"
        self.claude_log.write_text("")
        claude = d / "bin" / "claude"
        claude.write_text(f'#!{py}\nimport os, sys\nopen(os.environ["FAKE_CLAUDE_LOG"], "a").write("called\\n")\nprint("x")\n')
        claude.chmod(0o755)
        (d / "stubs" / "mlx_whisper.py").write_text("def transcribe(*a, **k):\n    raise RuntimeError('not used')\n")
        if key is not None:
            (d / "secrets" / "device_key").write_text(key + "\n")
            os.chmod(d / "secrets" / "device_key", 0o600)
        if db_bytes is not None:
            (d / "data" / "panel.db").write_bytes(db_bytes)
        if db_version is not None:
            con = sqlite3.connect(d / "data" / "panel.db")
            con.execute(f"PRAGMA user_version = {db_version}")
            con.close()
        if seed_v070:                                   # a database exactly as the v0.7.0 server left it
            import importlib.util
            source = subprocess.run(["git", "show", "v0.7.0:settings_store.py"], cwd=ROOT, capture_output=True,
                                    text=True, check=True).stdout
            (d / "old_store.py").write_text(source)
            spec = importlib.util.spec_from_file_location("old_store", d / "old_store.py")
            old = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old)
            store = old.Store(d / "data" / "panel.db")
            store.set_brightness(70)
            store.save_report({"app": "0.7.0-dev", "store": store.desired()[0], "rev": 1, "brightness": 70}, time.time() - 20)
        if seed_v080:                                   # a database exactly as the v0.8.0 server left it (schema minor 2)
            import importlib.util
            source = subprocess.run(["git", "show", "v0.8.0:settings_store.py"], cwd=ROOT, capture_output=True,
                                    text=True, check=True).stdout
            (d / "old_store080.py").write_text(source)
            spec = importlib.util.spec_from_file_location("old_store080", d / "old_store080.py")
            old = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old)
            store = old.Store(d / "data" / "panel.db")
            store.set_setting("voice_volume", 40)
            store.add_command("sleep", 45)
            store.save_report({"app": "0.8.0", "store": store.desired()[0], "rev": 1, "caps": ["brightness", "volume", "sleep"]},
                              time.time() - 20)
        if fake_tts:                                    # the local TTS pipeline replaced by two tiny scripts (no model runs)
            (d / "tts-env312" / "bin").mkdir(parents=True)
            fake = d / "tts-env312" / "bin" / "python"
            fake.write_text(f'#!{py}\nimport sys, wave, os\nif os.environ.get("FAKE_TTS_FAIL"):\n    sys.exit(3)\n'
                            'out = sys.argv[sys.argv.index("--output") + 1]\nw = wave.open(out, "wb")\n'
                            'w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(b"\\0\\0" * 22050)\nw.close()\n')
            fake.chmod(0o755)
            ffmpeg = d / "bin" / "ffmpeg"
            ffmpeg.write_text(f'#!{py}\nimport shutil, sys\nshutil.copy(sys.argv[sys.argv.index("-i") + 1], sys.argv[-1])\n')
            ffmpeg.chmod(0o755)
        self.device_port = free_port()
        self.panel_port = free_port()
        self.panel_expected = panel
        (d / "host.conf").write_text("# test: every value comes from the environment, never from a developer's own config/host.conf\n")
        self.env = dict(os.environ)
        self.env.update({"PATH": f"{d / 'bin'}:{self.env['PATH']}", "PYTHONPATH": isolation_guard.child_pythonpath(str(d / "stubs"), str(ROOT)),
                         "FAKE_CLAUDE_LOG": str(self.claude_log), "STICK_DATA_DIR": str(d / "data"),
                         "STICK_SECRETS_DIR": str(d / "secrets"), "STICK_PANEL_PORT": str(self.panel_port),
                         # the host configuration (docs/configuration.md) comes from the environment: no source patching, no config file
                         "STICK_CONFIG": str(d / "host.conf"), "STICK_ADVERTISE_HOST": "192.0.2.10", "STICK_BIND_HOST": "127.0.0.1",
                         "STICK_DEVICE_PORT": str(self.device_port), "STICK_RECORDINGS_DIR": str(d / "recordings"),
                         "STICK_TTS_ENV": str(d / "tts-env312")})
        if not panel:
            self.env["STICK_PANEL"] = "0"
        self.env.update(env_extra or {})
        self.blocker = None
        if busy_panel_port:
            self.blocker = socket.socket()
            self.blocker.bind(("127.0.0.1", self.panel_port))
            self.blocker.listen(1)
        self.seen = []                                # every response body and header block, for secret scanning
        self.proc = None
        self.start()

    def start(self):
        self.log_path = self.dir_path / f"server{time.time_ns()}.log"
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "audio_server.py")], env=self.env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", self.device_port), timeout=0.3).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise SystemExit("server did not start:\n" + self.log_path.read_text())
        time.sleep(0.3)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None
            self.log.close()

    def restart(self):
        self.stop()
        self.start()

    def close(self):
        self.stop()
        if self.blocker:
            self.blocker.close()

    def server_log(self):
        return "".join(p.read_text() for p in sorted(self.dir_path.glob("server*.log")))

    # ---- raw HTTP with full control of headers --------------------------------------------
    def http(self, port, method, path, headers=None, body=None, host=None):
        headers = dict(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        if body is not None and "Content-Length" not in headers:
            headers["Content-Length"] = str(len(body))
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders(body)
        r = conn.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        self.seen.append(json.dumps(hdrs) + data.decode("utf-8", "replace"))
        return r.status, hdrs, data

    def panel(self, method, path, **kw):
        return self.http(self.panel_port, method, path, **kw)

    def device(self, method, path, **kw):
        return self.http(self.device_port, method, path, **kw)

    def token(self):
        status, _, body = self.panel("GET", "/")
        return re.search(rb'name="csrf" content="([^"]+)"', body).group(1).decode()

    def set_brightness(self, value, token=None, **overrides):
        origin = f"http://127.0.0.1:{self.panel_port}"
        headers = {"Content-Type": "application/json", "Origin": origin, "X-CSRF-Token": token or self.token()}
        headers.update(overrides)
        return self.panel("POST", "/api/brightness", headers=headers, body={"value": value})

    def status(self):
        return json.loads(self.panel("GET", "/api/status")[2])

    def post(self, path, body, token=None, **overrides):
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.panel_port}",
                   "X-CSRF-Token": token or self.token()}
        headers.update(overrides)
        return self.panel("POST", path, headers=headers, body=body)

    def sync(self, key=KEY, **fields):
        body = {"proto": 1, "store": "", "rev": 0, "notif": 0}
        body.update(fields)
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["X-Device-Key"] = key
        status, _, data = self.device("POST", "/device/sync", headers=headers, body=body)
        return status, (json.loads(data) if data.startswith(b"{") else None)

    def db(self, sql, args=()):
        """Direct database access to move a reminder's time (simulated clock) and to inspect rows."""
        con = sqlite3.connect(self.dir_path / "data" / "panel.db", timeout=10)
        try:
            rows = con.execute(sql, args).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    def wait_for(self, predicate, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        raise AssertionError("timed out waiting")

    def claude_calls(self):
        return self.claude_log.read_text().count("called")


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def panel_listens_on_loopback_only(h):
    assert h.panel("GET", "/api/status")[0] == 200
    lan = lan_address()
    if lan:
        try:
            socket.create_connection((lan, h.panel_port), timeout=2).close()
        except OSError:
            return
        raise AssertionError(f"panel reachable on LAN address {lan}")


@case
def page_is_rtl_with_a_token_and_strict_headers(h):
    status, headers, body = h.panel("GET", "/")
    assert status == 200 and b'lang="he" dir="rtl"' in body and b"__CSRF__" not in body
    assert len(h.token()) >= 24
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert headers["cache-control"] == "no-store" and headers["x-frame-options"] == "DENY"
    assert "access-control-allow-origin" not in headers
    assert h.panel("GET", "/app.js")[0] == 200 and h.panel("GET", "/app.css")[0] == 200
    assert h.panel("GET", "/../audio_server.py")[0] == 404 and h.panel("GET", "/nope")[0] == 404


@case
def other_host_headers_are_rejected(h):
    for path in ("/", "/api/status", "/app.js"):
        assert h.panel("GET", path, host="evil.example")[0] == 403, path
        assert h.panel("GET", path, host=f"127.0.0.1:{h.panel_port + 1}")[0] == 403, path
    token = h.token()
    status, _, _ = h.panel("POST", "/api/brightness", host="evil.example", body={"value": 50},
                           headers={"Content-Type": "application/json", "X-CSRF-Token": token,
                                    "Origin": f"http://127.0.0.1:{h.panel_port}"})
    assert status == 403
    assert h.panel("GET", "/api/status", host=f"localhost:{h.panel_port}")[0] == 200


@case
def post_needs_origin_token_and_json(h):
    token, origin = h.token(), f"http://127.0.0.1:{h.panel_port}"
    good = {"Content-Type": "application/json", "Origin": origin, "X-CSRF-Token": token}
    before = h.status()["brightness"]["rev"]

    def post(headers, body=b'{"value": 50}'):
        return h.panel("POST", "/api/brightness", headers=headers, body=body)[0]

    assert post({k: v for k, v in good.items() if k != "Origin"}) == 403
    assert post({**good, "Origin": "http://evil.example"}) == 403
    assert post({**good, "Origin": "null"}) == 403
    assert post({**good, "Sec-Fetch-Site": "cross-site"}) == 403
    assert post({k: v for k, v in good.items() if k != "X-CSRF-Token"}) == 403
    assert post({**good, "X-CSRF-Token": "wrong"}) == 403
    assert post({**good, "X-CSRF-Token": token[:-1]}) == 403
    assert post({**good, "Content-Type": "text/plain"}) == 415
    assert post({**good, "Content-Type": "application/x-www-form-urlencoded"}, b"value=50") == 415
    assert post({k: v for k, v in good.items()}, b"x" * 300) == 413
    assert post(good, b"{not json") == 400
    assert post(good, b'{"nothing": 1}') == 400
    assert h.status()["brightness"]["rev"] == before                       # none of that changed anything
    assert post(good) == 200


@case
def invalid_brightness_values_are_rejected(h):
    token = h.token()
    before = h.status()["brightness"]
    for bad in (4, 101, 0, -5, "70", 70.5, True, None, [70], {"v": 1}):
        status, _, _ = h.set_brightness(bad, token=token)
        assert status == 400, (bad, status)
        time.sleep(0.15)                                                   # stay under the rate limit
    assert h.status()["brightness"]["rev"] == before["rev"]


@case
def change_is_stored_and_shown_as_pending_until_the_device_reports(h):
    time.sleep(1.2)
    status, _, body = h.set_brightness(37)
    snap = json.loads(body)
    assert status == 200 and snap["brightness"]["desired"] == 37 and snap["brightness"]["state"] == "pending"
    assert snap["device"]["contact"] == "never"
    assert h.status()["brightness"]["state"] == "pending"


@case
def rapid_changes_are_rate_limited_and_the_latest_value_wins_after_a_pause(h):
    token = h.token()
    time.sleep(1.5)                                                        # refill the bucket
    codes, applied = [], []
    for value in range(10, 50):                                            # a very fast drag
        status, headers, _ = h.set_brightness(value, token=token)
        codes.append(status)
        if status == 200:
            applied.append(value)
        else:
            assert status == 429 and headers.get("retry-after") == "1"
    assert 429 in codes and codes[0] == 200                                # limited, but not blocked
    assert h.status()["brightness"]["desired"] == applied[-1] != 49        # some intermediate values never arrived
    time.sleep(1.2)
    assert h.set_brightness(49, token=token)[0] == 200                     # the client's retry of its latest value
    assert h.status()["brightness"]["desired"] == 49


@case
def device_sync_requires_the_key_and_failures_change_nothing(h):
    state = h.status()
    for key in (None, "", "wrong", KEY[:-1], KEY + "x"):
        status, body = h.sync(key=key, store="x", rev=99, brightness=70)
        assert status == 401 and body == {"error": "unauthorized"}, (key, status)
    after = h.status()
    assert after["device"]["contact"] == state["device"]["contact"] and after["brightness"]["rev"] == state["brightness"]["rev"]
    assert h.sync(key=KEY)[0] == 200


@case
def confirmation_lifecycle_over_http(h):
    time.sleep(1.2)
    h.set_brightness(61)
    store = h.status()["brightness"]["store"]
    rev = h.status()["brightness"]["rev"]
    status, answer = h.sync(store="", rev=0, app="0.7.0", fw="V2.4.8", battery=90, charging=False, rssi=-55, uptime=12)
    assert status == 200 and answer["settings"]["brightness"] == 61 and answer["rev"] == rev and answer["store"] == store
    assert answer["poll_ms"] == 1000                                       # fast while a change is unconfirmed
    snap = h.status()
    assert snap["device"]["contact"] == "fresh" and snap["brightness"]["state"] == "pending"
    assert snap["device"]["report"]["app"] == "0.7.0" and snap["device"]["report"]["battery"] == 90
    h.sync(store=store, rev=rev, brightness=61)
    snap = h.status()
    assert snap["brightness"]["state"] == "confirmed" and snap["brightness"]["reported"] == 61
    h.sync(store=store, rev=rev, brightness=50)
    assert h.status()["brightness"]["state"] == "differs"
    h.sync(store="ffffffffffffffff", rev=rev, brightness=61)               # same revision from another store
    assert h.status()["brightness"]["state"] == "pending"
    assert h.sync(store=store, rev=rev, brightness=61)[1]["poll_ms"] in (1000, 3000)


@case
def concurrent_syncs_are_all_answered_consistently(h):
    results, errors = [], []

    def worker():
        try:
            for _ in range(8):
                results.append(h.sync(KEY)[0])
        except Exception as error:                                          # noqa: BLE001
            errors.append(repr(error))

    # 4 clients at once. (Python's default listen backlog is 5, so a 10-way simultaneous burst resets
    # connections on the v0.6.0 server too; real load is one Stick plus a browser tab.)
    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and results.count(200) == 32, (errors[:2], results.count(200))


@case
def the_old_unauthenticated_routes_are_gone_and_the_hook_uses_the_loopback_panel(h):
    assert h.device("GET", "/notify/next?after=0")[0] == 404 and h.device("GET", "/notify/next?after=0", headers=AUTH)[0] == 404
    assert h.device("GET", "/status?id=0")[0] == 401 and h.device("GET", "/status?id=0", headers=AUTH)[0] == 200
    event = {"kind": "turn_finished", "label": "proj", "session": "s", "prompt": "p"}
    assert h.device("POST", "/notify", headers={"Content-Type": "application/json"}, body=event)[0] == 404
    status, _, body = h.panel("POST", "/hook/notify", headers=HOOK, body=event)
    assert status == 200 and json.loads(body)["queued"] is True


@case
def notifications_ride_on_the_sync_with_the_same_cursor(h):
    status, answer = h.sync(notif=0)
    note = answer["notification"]
    assert status == 200 and note["label"] == "proj"
    assert h.sync(notif=note["id"])[1]["notification"] is None             # acknowledged by the cursor


@case
def restart_keeps_state_and_reports_the_real_age(h):
    time.sleep(1.2)
    h.set_brightness(44)
    store = h.status()["brightness"]["store"]
    rev = h.status()["brightness"]["rev"]
    h.sync(store=store, rev=rev, brightness=44, app="0.7.0")
    h.restart()
    snap = h.status()
    assert snap["brightness"]["desired"] == 44 and snap["brightness"]["rev"] == rev and snap["brightness"]["store"] == store
    assert snap["device"]["contact"] in ("fresh", "gap") and snap["device"]["report"]["app"] == "0.7.0"
    h.stop()
    con = sqlite3.connect(h.dir_path / "data" / "panel.db")                # make that saved report old
    con.execute("UPDATE device_report SET received_at = ?", (time.time() - 100,))
    con.commit(); con.close()
    h.start()
    snap = h.status()
    assert snap["device"]["contact"] == "gap" and 99 < snap["device"]["age_s"] < 110, snap["device"]
    assert snap["brightness"]["state"] == "confirmed"                      # honest: confirmed 100 s ago
    h.stop()
    con = sqlite3.connect(h.dir_path / "data" / "panel.db")
    con.execute("UPDATE device_report SET received_at = ?", (time.time() - 400,))
    con.commit(); con.close()
    h.start()
    assert h.status()["device"]["contact"] == "stale"
    status, answer = h.sync(store=store, rev=rev - 1 if rev > 0 else 0, brightness=1)   # an older Stick catches up
    assert answer["settings"]["brightness"] == 44 and answer["rev"] == rev


@case
def no_ai_calls_and_no_secret_in_any_output(h):
    assert h.claude_calls() == 0, "the panel must never call an AI model"
    blob = "".join(h.seen) + h.server_log()
    assert KEY not in blob
    assert "X-Device-Key" not in h.server_log()


CAPS = ["brightness", "volume", "sleep"]


@case
def settings_endpoint_accepts_valid_values_and_rejects_everything_else(h):
    token = h.token()
    time.sleep(1.2)
    for key, value in (("voice_volume", 0), ("voice_volume", 100), ("chime_volume", 27), ("idle_timeout_s", 30),
                       ("idle_timeout_s", 0), ("sleep_mode", "off"), ("sleep_mode", "dim")):
        assert h.post("/api/setting", {"key": key, "value": value}, token=token)[0] == 200, (key, value)
        time.sleep(0.15)
    rev = h.status()["brightness"]["rev"]
    for body in ({"key": "voice_volume", "value": 101}, {"key": "voice_volume", "value": -1}, {"key": "voice_volume", "value": "50"},
                 {"key": "voice_volume", "value": True}, {"key": "chime_volume", "value": 50.5}, {"key": "idle_timeout_s", "value": 45},
                 {"key": "idle_timeout_s", "value": "30"}, {"key": "sleep_mode", "value": "nap"}, {"key": "sleep_mode", "value": 1},
                 {"key": "volume", "value": 5}, {"key": ["voice_volume"], "value": 5}, {"key": None, "value": 5},
                 {"value": 5}, {"key": "voice_volume"}, {}, []):
        status = h.post("/api/setting", body, token=token)[0]
        assert status == 400, (body, status)
        time.sleep(0.15)
    assert h.status()["brightness"]["rev"] == rev                            # nothing invalid changed anything
    st = h.status()["settings"]
    assert st["voice_volume"]["desired"] == 100 and st["idle_timeout_s"]["desired"] == 0 and st["sleep_mode"]["desired"] == "dim"


@case
def settings_and_command_endpoints_have_the_same_security_as_brightness(h):
    token, origin = h.token(), f"http://127.0.0.1:{h.panel_port}"
    for path, body in (("/api/setting", {"key": "voice_volume", "value": 10}), ("/api/command", {"type": "sleep"})):
        good = {"Content-Type": "application/json", "Origin": origin, "X-CSRF-Token": token}

        def post(headers, raw=None):
            return h.panel("POST", path, headers=headers, body=body if raw is None else raw)[0]

        assert post({k: v for k, v in good.items() if k != "Origin"}) == 403
        assert post({**good, "Origin": "http://evil.example"}) == 403
        assert post({**good, "X-CSRF-Token": "x"}) == 403
        assert post({k: v for k, v in good.items() if k != "X-CSRF-Token"}) == 403
        assert post({**good, "Content-Type": "text/plain"}) == 415
        assert h.panel("POST", path, headers=good, body=body, host="evil.example")[0] == 403
        assert post(good, raw=b"x" * 400) == 413
    assert h.status()["command"] is None or h.status()["command"]["type"] != "sleep"      # none of that queued a command


@case
def command_endpoint_validates_and_queues_one_shot_commands(h):
    token = h.token()
    time.sleep(1.2)
    for bad in ({"type": "reboot"}, {"type": ""}, {"type": None}, {"type": ["sleep"]}, {"type": 5}, {}, []):
        assert h.post("/api/command", bad, token=token)[0] == 400, bad
        time.sleep(0.15)
    status, _, body = h.post("/api/command", {"type": "sleep"}, token=token)
    snap = json.loads(body)
    assert status == 200 and snap["command"]["type"] == "sleep" and snap["command"]["state"] == "queued"
    assert 40 < snap["command"]["ttl_left_s"] <= 45


@case
def command_and_settings_flow_end_to_end_over_http(h):
    time.sleep(1.2)
    token = h.token()
    h.post("/api/setting", {"key": "voice_volume", "value": 0}, token=token)
    time.sleep(0.3)
    h.post("/api/command", {"type": "wake"}, token=token)
    status, answer = h.sync(store="", rev=0, boot="bootX", caps=CAPS, screen="dim")
    assert status == 200 and answer["settings"]["voice_volume"] == 0
    cmd = answer["commands"][0]
    assert cmd["type"] == "wake" and 0 < cmd["ttl_ms"] <= 45000 and answer["poll_ms"] == 1000
    assert h.status()["command"]["state"] == "sent" and h.status()["screen"] == "dim"
    store, rev = answer["store"], answer["rev"]
    h.sync(store=store, rev=rev, boot="bootX", caps=CAPS, screen="awake", voice_volume=0, cmds=[[cmd["id"], "executed"]])
    snap = h.status()
    assert snap["command"]["state"] == "executed" and snap["screen"] == "awake"
    assert snap["settings"]["voice_volume"]["state"] == "confirmed" and snap["settings"]["voice_volume"]["reported"] == 0
    assert h.sync(store=store, rev=rev, boot="bootX", caps=CAPS)[1]["commands"] == []


@case
def an_old_stick_without_capabilities_never_receives_commands(h):
    time.sleep(1.2)
    h.post("/api/command", {"type": "sleep"})
    status, answer = h.sync(store="", rev=0, boot="oldstick")                # a v0.7.0 report: no caps
    assert status == 200 and answer["commands"] == []
    assert h.status()["caps"] == ["brightness"] and h.status()["command"]["state"] == "queued"


@case
def sleep_and_volume_requests_make_no_ai_calls(h):
    assert h.claude_calls() == 0



# ---- reminders (v0.9.0) -----------------------------------------------------------------------------

CAPS3 = CAPS + ["reminder"]


def rpost(h, path, body, **kw):
    """h.post that waits out the panel's own rate limit (these tests make many requests in a row)."""
    for _ in range(12):
        result = h.post(path, body, **kw)
        if result[0] != 429:
            return result
        time.sleep(0.5)
    return result


def new_reminder(h, mode="chime", text="Call Dan", minutes=30, **extra):
    status, _, data = rpost(h, "/api/reminders", {"text": text, "mode": mode, "when": {"kind": "in", "minutes": minutes}, **extra})
    assert status == 200, data
    return json.loads(data)["reminders"]["pending"][-1]


def due_now(h, item, seconds_ago=1):
    h.db("UPDATE reminders SET due_at = ? WHERE id = ?", (time.time() - seconds_ago, item["id"]))


def clear_reminders(h):
    h.db("DELETE FROM reminders")


@case
def reminder_endpoints_have_the_same_security_as_the_other_posts(h):
    token, origin = h.token(), f"http://127.0.0.1:{h.panel_port}"
    good = {"Content-Type": "application/json", "Origin": origin, "X-CSRF-Token": token}
    body = {"text": "x", "mode": "chime", "when": {"kind": "in", "minutes": 5}}
    for path in ("/api/reminders", "/api/reminders/preview", "/api/reminders/edit", "/api/reminders/cancel"):
        assert h.panel("POST", path, headers={k: v for k, v in good.items() if k != "X-CSRF-Token"}, body=body)[0] == 403
        assert h.panel("POST", path, headers={**good, "Origin": "http://evil.example"}, body=body)[0] == 403
        assert h.panel("POST", path, headers={**good, "Content-Type": "text/plain"}, body=body)[0] == 415
        assert h.panel("POST", path, headers=good, body=b"x" * 5000)[0] == 413
        assert h.panel("POST", path, headers=good, body=b"[1, 2]")[0] == 400
        assert h.panel("POST", path, headers=good, body=b"not json")[0] == 400
    assert h.panel("POST", "/api/reminders/other", headers=good, body=body)[0] == 404
    assert h.panel("POST", "/api/reminders", headers=good, body=body, host="evil.example")[0] == 403
    assert not h.db("SELECT 1 FROM reminders")                                  # none of that stored anything


@case
def preview_resolves_to_jerusalem_time_and_reports_dst_problems_explicitly(h):
    status, _, data = rpost(h, "/api/reminders/preview", {"when": {"kind": "at", "local": "2026-12-01T09:30"}})
    info = json.loads(data)["preview"]
    assert status == 200 and info["local"] == "2026-12-01 09:30" and info["offset"] == "+02:00" and info["tz"] == "Asia/Jerusalem"
    status, _, data = rpost(h, "/api/reminders/preview", {"when": {"kind": "in", "minutes": 10}})
    assert status == 200 and abs(json.loads(data)["preview"]["due_at"] - (time.time() + 600)) < 5
    status, _, data = rpost(h, "/api/reminders/preview", {"when": {"kind": "at", "local": "2027-03-26T02:30"}})
    assert status == 400 and json.loads(data)["code"] == "nonexistent"
    overlap = next_autumn_overlap()
    if overlap:
        status, _, data = rpost(h, "/api/reminders/preview", {"when": {"kind": "at", "local": overlap}})
        answer = json.loads(data)
        assert status == 400 and answer["code"] == "ambiguous" and [o["offset"] for o in answer["options"]] == ["+03:00", "+02:00"], answer
        status, _, data = rpost(h, "/api/reminders/preview", {"when": {"kind": "at", "local": overlap}, "fold": 1})
        assert status == 200 and json.loads(data)["preview"]["offset"] == "+02:00"
    status, _, data = rpost(h, "/api/reminders/preview", {"when": {"kind": "at", "local": "2020-01-01T10:00"}})
    assert status == 400 and json.loads(data)["code"] == "past"
    assert not h.db("SELECT 1 FROM reminders")


def next_autumn_overlap():
    """The repeated 01:30 of the next clock change back (last Sunday of October), as a local time string, in the future."""
    import datetime
    now = time.time()
    for year in range(datetime.date.today().year, datetime.date.today().year + 3):
        day = datetime.date(year, 10, 31)
        while day.weekday() != 6:
            day -= datetime.timedelta(days=1)
        stamp = datetime.datetime(year, day.month, day.day).timestamp()
        if now + 86400 < stamp < now + 360 * 86400:
            return f"{day.isoformat()}T01:30"
    return None                      # none within the one-year horizon right now (a few weeks each year); the unit tests cover it


@case
def create_edit_cancel_over_http_with_versioned_audio(h):
    clear_reminders(h)
    item = new_reminder(h, "speak", "שלום עולם", 30, client_key="same-key")
    again = new_reminder(h, "speak", "שלום עולם", 30, client_key="same-key")      # a retried request: no duplicate
    assert again["id"] == item["id"] and len(h.status()["reminders"]["pending"]) == 1
    ready = h.wait_for(lambda: next((r for r in h.status()["reminders"]["pending"] if r["audio"] == "ready"), None))
    audio_dir = h.dir_path / "data" / "reminders"
    assert [p.name for p in audio_dir.glob("*")] == [f"r{item['id']}_o{item['occ']}.wav"] and ready["mode"] == "speak"
    status, _, data = rpost(h, "/api/reminders/edit", {"id": item["id"], "occ": item["occ"], "text": "שלום מחדש", "mode": "speak",
                                                     "when": {"kind": "in", "minutes": 45}})
    edited = json.loads(data)["reminders"]["pending"][0]
    assert status == 200 and edited["occ"] != item["occ"] and edited["text"] == "שלום מחדש"
    h.wait_for(lambda: [p.name for p in audio_dir.glob("*")] == [f"r{item['id']}_o{edited['occ']}.wav"])   # the old audio is gone
    assert rpost(h, "/api/reminders/edit", {"id": item["id"], "occ": item["occ"], "text": "x", "mode": "chime",
                                          "when": {"kind": "in", "minutes": 9}})[0] == 409      # a stale version
    assert rpost(h, "/api/reminders/cancel", {"id": item["id"], "occ": item["occ"]})[0] == 409
    status, _, data = rpost(h, "/api/reminders/cancel", {"id": item["id"], "occ": edited["occ"]})
    snap = json.loads(data)["reminders"]
    assert status == 200 and snap["pending"] == [] and snap["history"][0]["status"] == "cancelled"
    assert not list(audio_dir.glob("*"))
    for bad in ({"text": "", "mode": "chime"}, {"text": "x", "mode": "shout", "when": {"kind": "in", "minutes": 5}},
                {"text": "x", "mode": "chime", "when": {"kind": "in", "minutes": 0}},
                {"text": "x", "mode": "chime", "when": {"kind": "at", "local": "2020-01-01T10:00"}},
                {"text": "x" * 201, "mode": "chime", "when": {"kind": "in", "minutes": 5}}):
        assert rpost(h, "/api/reminders", bad)[0] == 400, bad
    assert rpost(h, "/api/reminders/cancel", {"id": 99999, "occ": 1})[0] == 404
    assert rpost(h, "/api/reminders/cancel", {"id": True, "occ": 1})[0] == 400


@case
def a_due_reminder_is_delivered_only_to_a_capable_stick_and_followed_to_the_end(h):
    clear_reminders(h)
    item = new_reminder(h, "chime", "Take pills", 30)
    due_now(h, item)
    assert h.sync(boot="old", caps=CAPS)[1]["reminder"] is None                  # a v0.8.0 Stick never receives it
    assert h.sync(boot="old")[1]["reminder"] is None                             # nor one that reports no capabilities
    reply = h.sync(boot="b1", caps=CAPS3)[1]["reminder"]
    assert reply["occ"] == item["occ"] and reply["mode"] == "chime" and reply["label"] == "Take pills" and reply["ttl_ms"] > 0
    assert h.status()["reminders"]["pending"][0]["status"] == "sent"
    hdr = {"Content-Type": "application/json"}
    body = {"occ": item["occ"], "state": "presented", "boot": "b1"}
    assert h.device("POST", "/device/reminder/state", headers=hdr, body=body)[0] == 401          # needs the key
    assert h.device("POST", "/device/reminder/state", headers={**hdr, "X-Device-Key": "x" * 40}, body=body)[0] == 401
    assert h.device("POST", "/device/reminder/state", headers={**hdr, "X-Device-Key": KEY}, body=body)[0] == 200
    assert h.status()["reminders"]["pending"][0]["status"] == "presented"
    # a sync that carries the final result (the immediate report may have been lost)
    h.sync(boot="b1", caps=CAPS3, rems=[[item["occ"], "completed"]])
    snap = h.status()["reminders"]
    assert snap["pending"] == [] and snap["history"][0]["status"] == "completed" and snap["history"][0]["late"] is False


@case
def reminder_audio_is_authenticated_current_and_removed_after_use(h):
    clear_reminders(h)
    item = new_reminder(h, "speak", "שלום", 30)
    h.wait_for(lambda: h.status()["reminders"]["pending"][0]["audio"] == "ready")
    url = f"/device/reminder.wav?occ={item['occ']}"
    assert h.device("GET", url, headers={"X-Device-Key": KEY})[0] == 404         # not delivered yet
    due_now(h, item)
    assert h.sync(boot="b1", caps=CAPS3)[1]["reminder"]["mode"] == "speak"
    assert h.device("GET", url)[0] == 401 and h.device("GET", url, headers={"X-Device-Key": "x" * 40})[0] == 401
    status, headers, data = h.device("GET", url, headers={"X-Device-Key": KEY})
    assert status == 200 and headers["content-type"] == "audio/wav" and data[:4] == b"RIFF" and len(data) > 1000
    assert h.status()["reminders"]["pending"][0]["status"] == "received"          # the download is receipt, not playback
    assert h.device("GET", f"/device/reminder.wav?occ={item['occ'] + 1}", headers={"X-Device-Key": KEY})[0] == 404
    assert h.device("GET", "/device/reminder.wav?occ=abc", headers={"X-Device-Key": KEY})[0] == 400
    h.sync(boot="b1", caps=CAPS3, rems=[[item["occ"], "dismissed"]])
    assert h.device("GET", url, headers={"X-Device-Key": KEY})[0] == 404         # finished: gone
    assert not list((h.dir_path / "data" / "reminders").glob("*"))


@case
def reminders_never_touch_answers_memory_or_the_ai(h):
    before = h.claude_calls()
    clear_reminders(h)
    item = new_reminder(h, "speak", "בדיקה", 30)
    h.wait_for(lambda: h.status()["reminders"]["pending"][0]["audio"] == "ready")
    due_now(h, item)
    h.sync(boot="b1", caps=CAPS3)
    recordings = h.dir_path / "recordings"
    assert not (recordings / "reply_for_stick.wav").exists() and not (recordings / "reply.wav").exists()
    assert h.claude_calls() == before
    clear_reminders(h)


@case
def an_overdue_reminder_beyond_the_grace_window_is_missed_and_never_delivered(h):
    clear_reminders(h)
    item = new_reminder(h, "chime", "Old news", 30)
    due_now(h, item, seconds_ago=31 * 60)
    assert h.sync(boot="b1", caps=CAPS3)[1]["reminder"] is None
    snap = h.status()["reminders"]
    assert snap["history"][0]["status"] == "missed" and snap["history"][0]["reason"] == "grace" and snap["pending"] == []
    late = new_reminder(h, "chime", "A bit late", 30)
    due_now(h, late, seconds_ago=20 * 60)
    got = h.sync(boot="b1", caps=CAPS3)[1]["reminder"]
    assert got["late_s"] >= 20 * 60 - 2 and h.status()["reminders"]["pending"][0]["late"] is True


@case
def reminder_requests_leave_the_secrets_out_of_every_response(h):
    clear_reminders(h)
    new_reminder(h, "chime", "x", 30)
    assert KEY not in h.status().__repr__()
    clear_reminders(h)

# ---- variants (continued) -------------------------------------------------------------------------


def variant_commands_expire_with_a_short_ttl_and_are_not_replayed_after_reconnect():
    h = Harness(env_extra={"STICK_COMMAND_TTL_S": "2"})
    try:
        h.post("/api/command", {"type": "sleep"})
        time.sleep(3.2)                                                       # the Stick was away longer than the expiry
        status, answer = h.sync(boot="b1", caps=CAPS)
        assert status == 200 and answer["commands"] == []
        snap = h.status()["command"]
        assert snap["state"] == "expired" and snap["detail"] == "not executed in time"
    finally:
        h.close()


def variant_a_restarted_stick_is_not_sent_the_old_command():
    h = Harness()
    try:
        h.post("/api/command", {"type": "sleep"})
        assert len(h.sync(boot="bootA", caps=CAPS)[1]["commands"]) == 1
        assert h.sync(boot="bootB", caps=CAPS)[1]["commands"] == []          # rebooted: the command died with boot A
        assert h.status()["command"]["state"] == "failed"
        h.restart()                                                           # even a server restart does not resurrect it
        assert h.sync(boot="bootB", caps=CAPS)[1]["commands"] == []
        assert h.status()["command"]["state"] == "failed"
    finally:
        h.close()





def variant_reminders_survive_a_server_restart_and_overdue_ones_are_delivered_late():
    h = Harness(fake_tts=True)
    try:
        a = new_reminder(h, "speak", "שלום", 30)
        b = new_reminder(h, "chime", "Later", 40)
        h.wait_for(lambda: all(r["audio"] in ("ready", "none") for r in h.status()["reminders"]["pending"]))
        h.restart()
        pending = h.status()["reminders"]["pending"]
        assert [r["id"] for r in pending] == [a["id"], b["id"]] and pending[0]["audio"] == "ready"
        h.stop()
        h.db("UPDATE reminders SET due_at = ? WHERE id = ?", (time.time() - 5 * 60, a["id"]))      # due while the server was down
        h.start()
        reply = h.sync(boot="b1", caps=CAPS3)[1]["reminder"]
        assert reply["occ"] == a["occ"] and reply["late_s"] >= 5 * 60 - 2 and reply["mode"] == "speak"
        assert not [p for p in (h.dir_path / "data" / "reminders").glob("*") if p.name.startswith("tmp_")]
    finally:
        h.close()


def variant_a_failing_voice_is_reported_and_never_blocks_the_reminder():
    h = Harness(fake_tts=True, env_extra={"FAKE_TTS_FAIL": "1"})
    try:
        item = new_reminder(h, "speak", "שלום", 30)
        h.wait_for(lambda: h.status()["reminders"]["pending"][0]["audio"] in ("pending", "failed"))
        time.sleep(1.0)
        assert not list((h.dir_path / "data" / "reminders").glob("*"))        # no half-written audio left behind
        h.db("UPDATE reminders SET due_at = ?, audio = 'failed' WHERE id = ?", (time.time() - 1, item["id"]))
        reply = h.sync(boot="b1", caps=CAPS3)[1]["reminder"]
        assert reply["mode"] == "chime"                                       # explicit fallback, never silent nothing
        h.sync(boot="b1", caps=CAPS3, rems=[[item["occ"], "completed"]])
        done = h.status()["reminders"]["history"][0]
        assert done["status"] == "completed" and done["reason"] == "voice_unavailable" and done["delivered_mode"] == "chime"
    finally:
        h.close()


def variant_panel_ui_folder_can_be_switched_without_code_changes():
    """STICK_PANEL_DIR serves another UI folder (frontend rollback / trying a new UI); the CSRF token is still injected and the API is unchanged."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in (("index.html", '<!doctype html><meta name="csrf" content="__CSRF__"><title>other ui</title>'),
                           ("app.js", "//"), ("app.css", "/**/"), ("reminders.js", "//")):
            (Path(tmp) / name).write_text(body)
        h = Harness(env_extra={"STICK_PANEL_DIR": tmp})
        try:
            status, headers, body = h.panel("GET", "/")
            assert status == 200 and b"other ui" in body and b"__CSRF__" not in body and len(h.token()) >= 24
            assert h.panel("GET", "/api/status")[0] == 200 and h.panel("GET", "/app.js")[1].get("content-type", "").startswith("text/javascript")
            assert h.post("/api/setting", {"key": "voice_volume", "value": 40})[0] == 200          # the API behind it is the same
        finally:
            h.close()


def variant_a_server_without_host_configuration_refuses_to_start_and_says_what_to_do():
    """A new installation with no configuration must get an actionable message, never a guessed address, and must open no port."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        panel_port, device_port = free_port(), free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("STICK_") and k != "MPREMOTE"}
        env.update({"PYTHONPATH": isolation_guard.child_pythonpath(str(Path(tmp) / "stubs"), str(ROOT)), "STICK_CONFIG": str(Path(tmp) / "none.conf"),
                    "STICK_DATA_DIR": str(Path(tmp) / "data"), "STICK_SECRETS_DIR": str(Path(tmp) / "secrets"), "STICK_RECORDINGS_DIR": str(Path(tmp) / "rec"),
                    "STICK_PANEL_PORT": str(panel_port), "STICK_DEVICE_PORT": str(device_port)})
        done = subprocess.run([sys.executable, "-u", str(ROOT / "audio_server.py")], env=env, capture_output=True, text=True, timeout=60)
        assert done.returncode == 2 and "STICK_CONFIG points to a file that does not exist" in done.stderr
        env["STICK_CONFIG"] = str(Path(tmp) / "present.conf")
        (Path(tmp) / "present.conf").write_text("# empty: no address\n")
        done = subprocess.run([sys.executable, "-u", str(ROOT / "audio_server.py")], env=env, capture_output=True, text=True, timeout=60)
        assert done.returncode == 2 and "advertise_host is not set" in done.stderr and "Edit " in done.stderr and "host.example.conf" in done.stderr, done.stderr
        for port in (panel_port, device_port):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                raise AssertionError(f"port {port} was opened")
            except OSError:
                pass
        assert not (Path(tmp) / "data").exists() and not (Path(tmp) / "rec").exists()          # it stopped before creating any state


def variant_the_server_listens_only_on_the_configured_address_and_ports_and_the_panel_stays_on_loopback():
    h = Harness(fake_tts=True)
    try:
        status, _, _ = h.device("GET", "/status?id=none", headers=AUTH)
        assert status == 200 and h.panel("GET", "/api/status")[0] == 200                      # the configured (alternate) ports answer
        lan = lan_address()
        if lan:
            for port in (h.device_port, h.panel_port):
                try:
                    socket.create_connection((lan, port), timeout=1).close()
                    raise AssertionError(f"reachable on the LAN address {lan}:{port}")        # bind_host is loopback in this test: nothing is exposed
                except OSError:
                    pass
    finally:
        h.close()

# ---- variants -----------------------------------------------------------------------------------

def variant_panel_disabled():
    h = Harness(panel=False)
    try:
        assert h.sync()[0] == 404                                          # no /device/sync: an old-server lookalike
        try:
            socket.create_connection(("127.0.0.1", h.panel_port), timeout=1).close()
            raise AssertionError("panel port is open with STICK_PANEL=0")
        except OSError:
            pass
        assert h.device("GET", "/status?id=0", headers=AUTH)[0] == 200 and h.device("GET", "/status?id=0")[0] == 401   # the key holds without the panel
    finally:
        h.close()


def variant_no_key_provisioned():
    h = Harness(key=None)
    try:
        assert h.sync()[0] == 401 and h.sync(key="anything")[0] == 401
        assert h.device("GET", "/status?id=0", headers=AUTH)[0] == 401 and h.device("POST", "/audio", headers=AUTH, body=bytes(32000))[0] == 401
        assert "device key NOT provisioned" in h.server_log()
        assert h.status()["key_provisioned"] is False
        assert h.set_brightness(70)[0] == 200                              # the panel still works and stores it
    finally:
        h.close()


def variant_corrupt_database():
    h = Harness(db_bytes=b"garbage that is not sqlite" * 100)
    try:
        assert "[panel] disabled" in h.server_log()
        assert h.sync()[0] == 404 and h.device("GET", "/status?id=0", headers=AUTH)[0] == 200   # the voice server runs on, still keyed
        assert h.device("GET", "/status?id=0")[0] == 401
        try:
            socket.create_connection(("127.0.0.1", h.panel_port), timeout=1).close()
            raise AssertionError("panel port is open with an unusable database")
        except OSError:
            pass
    finally:
        h.close()


def variant_newer_database_after_a_rollback():
    h = Harness(db_version=99)
    try:
        log = h.server_log()
        assert "[panel] disabled" in log and "newer" in log
        assert h.sync()[0] == 404
    finally:
        h.close()


def variant_panel_port_busy():
    h = Harness(busy_panel_port=True)
    try:
        assert "could not start" in h.server_log()
        assert h.device("GET", "/status?id=0", headers=AUTH)[0] == 200     # the voice server is unaffected
        assert h.sync()[0] == 200                                          # the device protocol still works
    finally:
        h.close()


def variant_lockout_after_repeated_bad_keys():
    h = Harness()
    try:
        for _ in range(20):
            assert h.sync(key="bad")[0] == 401
        assert h.sync(key=KEY)[0] == 429
        assert h.status()["device"]["contact"] == "never"
    finally:
        h.close()


VARIANTS = [variant_commands_expire_with_a_short_ttl_and_are_not_replayed_after_reconnect,
            variant_a_restarted_stick_is_not_sent_the_old_command,
            variant_reminders_survive_a_server_restart_and_overdue_ones_are_delivered_late,
            variant_a_failing_voice_is_reported_and_never_blocks_the_reminder, variant_panel_ui_folder_can_be_switched_without_code_changes, variant_a_server_without_host_configuration_refuses_to_start_and_says_what_to_do, variant_the_server_listens_only_on_the_configured_address_and_ports_and_the_panel_stays_on_loopback, variant_panel_disabled, variant_no_key_provisioned, variant_corrupt_database,
            variant_newer_database_after_a_rollback, variant_panel_port_busy, variant_lockout_after_repeated_bad_keys]


def main():
    failed = total = 0
    h = Harness(fake_tts=True)
    try:
        for fn in CASES:
            total += 1
            try:
                fn(h)
                print("PASS", fn.__name__, flush=True)
            except AssertionError as error:
                failed += 1
                print("FAIL", fn.__name__, str(error)[:400], flush=True)
            except Exception as error:                                      # noqa: BLE001
                failed += 1
                print("ERROR", fn.__name__, repr(error)[:400], flush=True)
    finally:
        h.close()
    for fn in VARIANTS:
        total += 1
        try:
            fn()
            print("PASS", fn.__name__, flush=True)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:400], flush=True)
        except Exception as error:                                          # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:400], flush=True)
    print(f"{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
