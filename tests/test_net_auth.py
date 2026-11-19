"""Network authentication of the distribution server (docs/security.md), against the REAL audio_server.py.

Run with the server's own interpreter (needs numpy/scipy): voice-env/bin/python tests/test_net_auth.py

Isolated: temporary data, secrets and recordings, test ports on 127.0.0.1, a synthetic key, and stand-ins for every AI piece
(tests/test_panel_server.py's Harness: a `claude` script that logs each call and a tiny TTS; here also an mlx_whisper stub that logs each
call). "No AI work" is checked from those logs, from the server's own log, and from the recordings folder."""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import test_conversation_server as tcs
import test_panel_server as tps

ROOT = tps.ROOT
KEY = tps.KEY
AUTH = {"X-Device-Key": KEY}
HOOK = {"Content-Type": "application/json", "X-Stick-Hook": "1"}
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class Harness(tps.Harness):
    """The real server with stand-ins; the Whisper stub counts its calls (and can be slow, to keep a job pending)."""

    def start(self):
        self.whisper_log = self.dir_path / "whisper_calls.log"
        self.whisper_log.touch()
        (self.dir_path / "stubs" / "mlx_whisper.py").write_text(
            "import os, time\ndef transcribe(path, **k):\n    open(os.environ['FAKE_WHISPER_LOG'], 'a').write('called\\n')\n"
            "    time.sleep(float(os.environ.get('FAKE_WHISPER_SLOW', '0')))\n    return {'text': 'שאלה'}\n")
        self.env["FAKE_WHISPER_LOG"] = str(self.whisper_log)
        super().start()

    def work(self):
        """Evidence of AI work so far: Whisper calls, Claude calls, recordings written, uploads read."""
        return (len(self.whisper_log.read_text().splitlines()), len(self.claude_log.read_text().splitlines()),
                sorted(p.name for p in (self.dir_path / "recordings").glob("*")), self.server_log().count("Receiving audio"))

    def upload(self, body, headers=AUTH):
        return self.device("POST", "/audio", headers=dict(headers, **{"Content-Type": "application/octet-stream"}), body=body)

    def wait_job(self, job_id, limit=60):
        end = time.time() + limit
        while time.time() < end:
            status, _, body = self.device("GET", f"/status?id={job_id}", headers=AUTH)
            state = json.loads(body)["state"]
            if state != "pending":
                return state
            time.sleep(0.2)
        raise AssertionError("still pending")


def with_harness(**kw):
    def wrap(fn):
        def run():
            h = Harness(fake_tts=True, **kw)
            try:
                fn(h)
                assert KEY not in h.server_log(), "the key appeared in the server log"
                assert not any(KEY in seen for seen in h.seen), "the key appeared in a response"
            finally:
                h.close()
        run.__name__ = fn.__name__
        return case(run)
    return wrap


LIFT = {"STICK_UPLOADS_PER_MIN": "100000", "STICK_MAX_PENDING_JOBS": "50"}


# ---- the authenticated path works ------------------------------------------------------------------------------------------
@with_harness(env_extra=LIFT)
def an_authenticated_question_runs_and_its_reply_downloads(h):
    status, _, body = h.upload(tcs.SPEECH)
    assert status == 200, body
    job = json.loads(body)["id"]
    assert h.wait_job(job) == "ready"
    status, headers, wav = h.device("GET", f"/reply.wav?id={job}", headers=AUTH)
    assert status == 200 and headers["content-type"] == "audio/wav" and wav[:4] == b"RIFF" and headers.get("cache-control") == "no-store"
    whisper, claude, _, _ = h.work()
    assert (whisper, claude) == (1, 1)


# ---- missing or wrong keys: refused before any work, nothing disclosed -------------------------------------------------------
@with_harness(env_extra=LIFT)
def every_lan_route_refuses_missing_and_wrong_keys_with_no_work_and_no_data(h):
    status, _, body = h.upload(tcs.SPEECH)
    job = json.loads(body)["id"]
    assert h.wait_job(job) == "ready"
    _, _, reply = h.device("GET", f"/reply.wav?id={job}", headers=AUTH)
    before = h.work()
    wrong = [{}, {"X-Device-Key": ""}, {"X-Device-Key": "w" * 40}, {"X-Device-Key": KEY[:-1]}, {"Authorization": "Bearer " + KEY},
             {"X-Device-Key": KEY.upper()}]
    routes = [("POST", "/audio", tcs.SPEECH), ("GET", f"/status?id={job}", None), ("GET", f"/reply.wav?id={job}", None),
              ("POST", "/device/sync", {"proto": 1, "store": "", "rev": 0, "notif": 0}), ("POST", "/device/reminder/state", {"occ": 1, "state": "shown"}),
              ("GET", "/device/reminder.wav?occ=1", None), ("GET", f"/status?id={job}&key={KEY}", None)]
    for headers in wrong:
        for method, path, body in routes:
            status, _, answer = h.device(method, path, headers=dict(headers), body=body)
            assert status in (401, 429), (method, path, headers.keys(), status)
            assert answer[:4] != b"RIFF" and reply not in answer and b"ready" not in answer and b"detail" not in answer, (path, answer[:80])
    assert h.work() == before, (before, h.work())


@with_harness(env_extra=LIFT)
def a_refused_upload_is_never_read(h):
    sock = socket.create_connection(("127.0.0.1", h.device_port), timeout=5)
    sock.sendall(b"POST /audio HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\nX-Device-Key: wrong-key-wrong-key-wrong-key-wrong\r\n\r\n")
    started = time.time()
    answer = sock.recv(4096)                                   # no body was sent: the answer must come anyway
    sock.close()
    assert answer.startswith(b"HTTP/1.0 401") and time.time() - started < 3, answer[:60]
    assert h.work()[1:] == (0, [], 0)


@with_harness(env_extra=LIFT)
def repeated_bad_keys_lock_that_client_out_of_every_route(h):
    for _ in range(25):
        h.device("GET", "/status?id=1", headers={"X-Device-Key": "x" * 40})
    for method, path, body in (("POST", "/audio", tcs.SPEECH), ("GET", "/status?id=1", None), ("POST", "/device/sync", {"proto": 1})):
        assert h.device(method, path, headers=AUTH, body=body)[0] == 429, path      # even the right key, for a while
    assert h.work()[1:] == (0, [], 0)


@case
def a_server_without_a_key_refuses_everything_and_says_so():
    h = Harness(fake_tts=True, key=None)
    try:
        for method, path, body in (("POST", "/audio", tcs.SPEECH), ("GET", "/status?id=1", None), ("GET", "/reply.wav?id=1", None)):
            assert h.device(method, path, headers=AUTH, body=body)[0] == 401
        assert "device key NOT provisioned" in h.server_log() and h.work()[1:] == (0, [], 0)
    finally:
        h.close()


# ---- the old unauthenticated paths are gone ----------------------------------------------------------------------------------
@with_harness(env_extra=LIFT)
def old_and_odd_paths_give_no_unauthenticated_way_in(h):
    for headers in ({}, AUTH):
        assert h.device("GET", "/notify/next?after=0", headers=headers)[0] == 404
        assert h.device("POST", "/notify", headers=dict(headers, **{"Content-Type": "application/json"}), body={"kind": "attention"})[0] == 404
    assert h.device("GET", "/reply.wav", headers=AUTH)[0] == 400               # never "the latest recording"
    assert h.device("GET", "/status", headers=AUTH)[0] == 400
    for path in ("/audio/", "/AUDIO", "/audio?x=1", "/audio#x", "/./audio"):
        assert h.device("POST", path, headers=AUTH, body=tcs.SILENCE)[0] == 404, path
    for path in ("/audio", "//audio", "/audio/", "/./audio", "/status?id=1", "//status?id=1", "/reply.wav?id=1", "//reply.wav?id=1"):
        method = "POST" if "audio" in path else "GET"
        assert h.device(method, path, body=tcs.SILENCE if method == "POST" else None)[0] in (401, 404), path   # never served without the key
    for method in ("PUT", "DELETE", "PATCH"):
        assert h.device(method, "/audio", headers=AUTH, body=tcs.SILENCE)[0] in (404, 405, 501)
    assert h.work()[1:] == (0, [], 0)
    assert h.device("POST", "/audio", headers={"X-Device-Key": "é" * 40}, body=tcs.SILENCE)[0] in (401, 429)   # not a crash
    assert "Traceback" not in h.server_log()


# ---- bounds ----------------------------------------------------------------------------------------------------------------------
@with_harness(env_extra=LIFT)
def sizes_are_checked_before_reading(h):
    assert h.device("POST", "/audio", headers=dict(AUTH, **{"Content-Length": "1000002"}), body=b"")[0] == 413
    assert h.device("POST", "/audio", headers=AUTH, body=bytes(1001))[0] == 400
    sock = socket.create_connection(("127.0.0.1", h.device_port), timeout=5)
    sock.sendall(b"POST /audio HTTP/1.1\r\nHost: x\r\nX-Device-Key: " + KEY.encode() + b"\r\n\r\n")
    assert sock.recv(4096).startswith(b"HTTP/1.0 411")
    sock.close()
    assert h.device("POST", "/device/sync", headers=dict(AUTH, **{"Content-Length": "5000"}), body=b"")[0] == 413
    assert h.work()[1:] == (0, [], 0)


@with_harness()
def uploads_are_rate_limited(h):
    codes = [h.upload(tcs.SILENCE)[0] for _ in range(5)]                         # default: a burst of 3, then 6 per minute
    assert codes[:3] == [200, 200, 200] and codes[3] == 429, codes


@with_harness(env_extra={"STICK_UPLOADS_PER_MIN": "100000", "STICK_MAX_PENDING_JOBS": "1", "FAKE_WHISPER_SLOW": "4"})
def the_queue_is_bounded_and_a_refused_upload_is_not_queued(h):
    assert h.upload(tcs.SPEECH)[0] == 200                                      # pending: Whisper is slow
    time.sleep(0.5)
    status, headers, _ = h.upload(tcs.SPEECH)
    assert status == 503 and headers.get("retry-after"), status
    time.sleep(6)
    assert h.work()[0] == 1 and h.server_log().count("Receiving audio") == 1


@with_harness(env_extra=dict(LIFT, STICK_REQUEST_TIMEOUT_S="2"))
def slow_or_idle_clients_cannot_hold_the_server(h):
    sock = socket.create_connection(("127.0.0.1", h.device_port), timeout=10)
    sock.sendall(b"POST /audio HTTP/1.1\r\nHost: x\r\nContent-Length: 64000\r\nX-Device-Key: " + KEY.encode() + b"\r\n\r\n" + bytes(1000))
    started = time.time()
    data = sock.recv(4096)                                                    # the rest never comes: the server gives up
    sock.close()
    assert time.time() - started < 8, "the server waited for the body"
    assert h.work()[0:2] == (0, 0)
    idle = [socket.create_connection(("127.0.0.1", h.device_port), timeout=5) for _ in range(16)]
    time.sleep(0.3)
    extra = socket.create_connection(("127.0.0.1", h.device_port), timeout=5)
    extra.sendall(b"GET /status?id=1 HTTP/1.0\r\nX-Device-Key: " + KEY.encode() + b"\r\n\r\n")
    try:
        assert extra.recv(100) == b"", "a 17th connection was served"        # closed at once (or reset)
    except ConnectionResetError:
        pass
    extra.close()
    time.sleep(3)                                                              # the idle ones time out
    assert h.device("GET", "/status?id=1", headers=AUTH)[0] == 200
    for s in idle:
        s.close()


# ---- the notification hook: loopback only, not from a browser -------------------------------------------------------------------
@with_harness(env_extra=LIFT)
def the_hook_works_on_loopback_and_its_notifications_reach_only_an_authenticated_stick(h):
    env = {k: v for k, v in h.env.items()}
    payload = json.dumps({"session_id": "s", "prompt_id": "p", "cwd": "/work/my-project"})
    done = subprocess.run([sys.executable, "-I", "-S", str(ROOT / "hooks" / "stick_notify.py"), "finished"], input=payload, capture_output=True,
                          text=True, env=env, timeout=15)
    assert done.returncode == 0
    status, answer = h.sync(notif=0)
    assert status == 200 and answer["notification"]["label"] == "my-project"
    assert h.device("POST", "/device/sync", body={"proto": 1, "store": "", "rev": 0, "notif": 0})[0] == 401
    event = {"kind": "attention", "label": "x"}
    assert h.panel("POST", "/hook/notify", headers={"Content-Type": "application/json"}, body=event)[0] == 403            # no hook header
    assert h.panel("POST", "/hook/notify", headers=dict(HOOK, Origin="http://evil.example"), body=event)[0] == 403    # from a web page
    assert h.panel("POST", "/hook/notify", headers={"Content-Type": "text/plain", "X-Stick-Hook": "1"}, body=event)[0] == 415
    assert h.panel("POST", "/hook/notify", headers=dict(HOOK, **{"Content-Length": "5000"}), body=b"")[0] == 413
    assert h.panel("POST", "/hook/notify", headers=HOOK, body=event, host="evil.example:80")[0] == 403              # DNS rebinding
    assert h.panel("POST", "/hook/notify", headers=HOOK, body=event)[0] == 200


# ---- the Stick's own code ---------------------------------------------------------------------------------------------------------
@case
def every_stick_request_carries_the_key_and_no_unauthenticated_route_is_used():
    voice = (ROOT / "stick" / "voice_send.py").read_text()
    calls = re.findall(r"requests\.(?:get|post)\((.*?)\)\n", voice, re.S)
    assert len(calls) >= 5 and all("auth_headers(" in c for c in calls), calls
    assert "if not poller.key:" in voice and '"No device key"' in voice
    device = (ROOT / "stick" / "device.py").read_text()
    code = "\n".join(line.split("#")[0] for line in device.split("class DeviceSync")[1].splitlines())
    assert "legacy" not in code and "/notify/next" not in code and "NotificationPoller._build_request" not in code
    assert "X-Device-Key: %s" in device


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:900])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:900])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
