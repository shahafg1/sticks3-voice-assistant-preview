"""Tests for stick/device.py (DeviceState and DeviceSync) on CPython.
Run: python3 tests/test_device_sync.py

DeviceSync runs against a scripted local HTTP server using the same non-blocking
socket code as on the Stick; DeviceState runs with a fake display.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import contextlib
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stick"))
import device  # noqa: E402
import notify  # noqa: E402

KEY = "A1b2C3d4" * 6          # 48 valid characters


class FakeLcd:
    def __init__(self, raw=255):
        self.raw, self.calls = raw, []

    def setBrightness(self, raw):
        self.calls.append(raw)
        self.raw = raw

    def getBrightness(self):
        return self.raw


class FakePower:
    def __init__(self):
        self.reads = 0

    def getBatteryLevel(self):
        self.reads += 1
        return 87

    def isCharging(self):
        return True


class FakeWlan:
    def status(self, what):
        assert what == "rssi"
        return -61


class Files:
    def __init__(self, **files):
        self.files, self.writes = dict(files), []

    def read(self, path):
        return self.files.get(path)

    def write(self, path, text):
        self.files[path] = text
        self.writes.append((path, text))

    def remove(self, path):
        self.files.pop(path, None)


def make_state(files=None, lcd=None, power=None, wlan=None):
    files = files or Files()
    state = device.DeviceState(lcd or FakeLcd(), power, wlan, "V2.4.8", read=files.read, write=files.write, remove=files.remove)
    return state, files


class Stub:
    """Scripted server. responses: {(method, path-prefix): (status, body) or callable(request) -> (status, body)}."""
    def __init__(self, responses):
        self.responses, self.requests = responses, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                           "body": body}
                outer.requests.append(request)
                for (method, prefix), response in outer.responses.items():
                    if method == self.command and self.path.startswith(prefix):
                        status, payload = response(request) if callable(response) else response
                        if status == "hang":
                            time.sleep(5)
                            return
                        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                        self.send_response(status)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                self.send_error(404)

            do_GET = do_POST = handle_any

            def log_message(self, *a):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.http.block_on_close = False
        self.port = self.http.server_port
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()

    def paths(self):
        return [(r["method"], r["path"].split("?")[0]) for r in self.requests]


def sync_ok(store="S1", rev=1, settings=None, poll_ms=3000, notification=None):
    return (200, {"store": store, "rev": rev, "settings": settings if settings is not None else {},
                  "notification": notification, "poll_ms": poll_ms})


def drive(poller, seconds, stop_when=None):
    notes, slowest = [], 0.0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        t = time.monotonic()
        note = poller.step()
        slowest = max(slowest, (time.monotonic() - t) * 1000)
        if note:
            notes.append(note)
            if stop_when and stop_when(note):
                break
        time.sleep(0.01)
    return notes, slowest


def make_sync(stub, files=None, key=KEY, interval_ms=40, **kw):
    state, files = make_state(files)
    return device.DeviceSync(state, "127.0.0.1", stub.port, interval_ms=interval_ms, key=key), state


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---- pure helpers and DeviceState ------------------------------------------------------------

@case
def percent_and_raw_round_trip_for_every_allowed_value():
    for p in range(device.BRIGHTNESS_MIN, device.BRIGHTNESS_MAX + 1):
        raw = device.pct_to_raw(p)
        assert 0 <= raw <= 255 and device.raw_to_pct(raw) == p, (p, raw)
    assert device.pct_to_raw(100) == 255 and device.pct_to_raw(5) == 13


@case
def brightness_is_clamped_so_the_screen_cannot_go_dark():
    state, _ = make_state()
    for bad, expected in ((0, 5), (-20, 5), (3, 5), (150, 100)):
        state.apply("S", bad + 1000, {"brightness": bad})
        assert state.lcd.raw == device.pct_to_raw(expected) and state.awake_pct == expected, (bad, state.lcd.raw)


@case
def apply_is_idempotent_and_follows_either_direction():
    state, _ = make_state()
    assert state.apply("S1", 3, {"brightness": 60}) is True
    assert state.apply("S1", 3, {"brightness": 60}) is False and len(state.lcd.calls) == 1
    assert state.apply("S1", 2, {"brightness": 40}) is True                 # an older revision (restored server)
    assert state.apply("S2", 2, {"brightness": 55}) is True                 # same revision, another store
    assert state.lcd.calls == [device.pct_to_raw(v) for v in (60, 40, 55)]


@case
def settings_without_brightness_record_the_revision_but_change_nothing():
    state, _ = make_state()
    assert state.apply("S1", 1, {}) is True and state.lcd.calls == []
    assert (state.store, state.rev) == ("S1", 1)
    for odd in (True, "70", 70.5, None, [70]):
        state.apply("S1", state.rev + 1, {"brightness": odd})
    assert state.lcd.calls == []                                              # wrong types are ignored


@case
def report_reads_the_value_back_from_the_display():
    state, _ = make_state(lcd=FakeLcd(raw=128), power=FakePower(), wlan=FakeWlan())
    state.apply("S1", 4, {"brightness": 70})
    r = state.report(77, now_ms=12000)
    assert r == {"proto": 1, "boot": state.boot, "app": device.APP_VERSION, "fw": "V2.4.8", "store": "S1", "rev": 4,
                 "notif": 77, "uptime": 12, "brightness": 70, "battery": 87, "charging": True, "rssi": -61,
                 "voice_volume": 100, "chime_volume": 27, "screen": "awake", "idle_timeout_s": 0, "sleep_mode": "dim",
                 "cmds": [], "rems": [], "caps": ["brightness", "volume", "sleep", "reminder"]}
    state.lcd.raw = 100                                                         # the display disagrees: report that
    assert state.report(0, now_ms=13000)["brightness"] == device.raw_to_pct(100)


@case
def optional_fields_are_left_out_when_apis_fail():
    class Broken:
        def getBatteryLevel(self):
            raise OSError("i2c")

        isCharging = getBatteryLevel

        def status(self, what):
            raise OSError("no wifi")

    lcd = FakeLcd()
    lcd.getBrightness = lambda: (_ for _ in ()).throw(OSError("gone"))
    state, _ = make_state(lcd=lcd, power=Broken(), wlan=Broken())
    r = state.report(0, now_ms=5000)
    assert set(r) == {"proto", "boot", "app", "fw", "store", "rev", "notif", "uptime", "voice_volume", "chime_volume",
                      "screen", "idle_timeout_s", "sleep_mode", "cmds", "rems", "caps"}, set(r)     # no brightness/battery/rssi


@case
def battery_is_read_at_most_every_30_seconds():
    power = FakePower()
    state, _ = make_state(power=power)
    for t in (0, 5000, 29000, 29999):
        state.report(0, now_ms=t)
    assert power.reads == 1
    state.report(0, now_ms=30000)
    assert power.reads == 2


@case
def cache_restores_brightness_at_boot_and_is_ignored_when_bad():
    files = Files(**{device.CACHE_FILE: json.dumps({"store": "S9", "rev": 5, "brightness": 30})})
    state, _ = make_state(files)
    assert (state.store, state.rev) == ("S9", 5) and state.lcd.calls == [device.pct_to_raw(30)]
    for bad in ("not json", "{}", '{"store": 1, "rev": 2, "brightness": 3}', '{"store": "x", "rev": "2", "brightness": 3}', ""):
        state, _ = make_state(Files(**{device.CACHE_FILE: bad}))
        assert state.lcd.calls == [] and (state.store, state.rev) == ("", 0), bad
    # a cache with a dangerous value is clamped, not applied raw
    state, _ = make_state(Files(**{device.CACHE_FILE: json.dumps({"store": "S", "rev": 1, "brightness": 0})}))
    assert state.lcd.calls == [device.pct_to_raw(5)]


@case
def cache_is_written_after_a_change_at_most_once_a_minute():
    state, files = make_state()
    state.maybe_save(0)
    assert files.writes == []                                                   # nothing to save yet
    state.apply("S1", 1, {"brightness": 70})
    state.maybe_save(1000)
    assert len(files.writes) == 1 and json.loads(files.writes[0][1]) == {
        "store": "S1", "rev": 1, "brightness": 70, "idle_timeout_s": 0, "sleep_mode": "dim"}
    state.apply("S1", 2, {"brightness": 50})
    state.maybe_save(20000)                                                     # too soon: held back
    state.maybe_save(59000)
    assert len(files.writes) == 1
    state.maybe_save(61000)
    assert len(files.writes) == 2 and json.loads(files.writes[1][1])["brightness"] == 50
    state.maybe_save(200000)
    assert len(files.writes) == 2                                               # clean: no more writes


@case
def failed_cache_write_never_raises():
    class Full(Files):
        def write(self, path, text):
            raise OSError("flash full")
    files = Full()
    state, _ = make_state(files)
    state.apply("S1", 1, {"brightness": 70})
    state.maybe_save(0)                                                         # must not raise
    assert state.dirty


# ---- DeviceSync: request format and replies ---------------------------------------------------

@case
def sync_request_is_a_post_with_the_key_in_the_header_only():
    stub = Stub({("POST", "/device/sync"): sync_ok()})
    p, state = make_sync(stub)
    state.apply("S0", 7, {"brightness": 60})
    p.ack(41)
    drive(p, 1.0, stop_when=lambda n: True)
    r = stub.requests[0]
    stub.close()
    assert (r["method"], r["path"]) == ("POST", "/device/sync")
    assert r["headers"]["x-device-key"] == KEY and r["headers"]["content-type"] == "application/json"
    assert int(r["headers"]["content-length"]) == len(r["body"])
    body = json.loads(r["body"])
    assert body["proto"] == 1 and body["store"] == "S0" and body["rev"] == 7 and body["notif"] == 41 and body["brightness"] == 60
    assert KEY.encode() not in r["body"]


@case
def reply_applies_settings_and_returns_the_notification():
    note = {"id": 9, "kind": "turn_finished", "label": "proj", "age": 0}
    stub = Stub({("POST", "/device/sync"): sync_ok(store="S1", rev=3, settings={"brightness": 45}, notification=note)})
    p, state = make_sync(stub)
    notes, _ = drive(p, 1.5, stop_when=lambda n: True)
    stub.close()
    assert notes == [note]
    assert (state.store, state.rev) == ("S1", 3) and state.lcd.calls == [device.pct_to_raw(45)]


@case
def old_notifications_are_not_shown_twice():
    note = {"id": 9, "kind": "turn_finished", "label": "proj", "age": 0}
    stub = Stub({("POST", "/device/sync"): sync_ok(notification=note)})
    p, _ = make_sync(stub)
    p.ack(9)
    notes, _ = drive(p, 0.5)
    stub.close()
    assert notes == []


@case
def server_poll_hint_is_adopted_and_bounded():
    for hint, expected in ((1000, 1000), (100, device.POLL_MIN_MS), (999999, device.POLL_MAX_MS)):
        stub = Stub({("POST", "/device/sync"): sync_ok(poll_ms=hint)})
        p, _ = make_sync(stub)
        drive(p, 0.6, stop_when=lambda n: False) if False else None
        end = time.monotonic() + 1.5
        while time.monotonic() < end and p.interval_ms == p.base_interval:
            p.step(); time.sleep(0.01)
        stub.close()
        assert p.interval_ms == expected, (hint, p.interval_ms)
    stub = Stub({("POST", "/device/sync"): (200, {"store": "S", "rev": 1, "settings": {}, "notification": None})})
    p, _ = make_sync(stub)
    p.interval_ms = 1000
    end = time.monotonic() + 1.5
    while time.monotonic() < end and p.interval_ms == 1000:
        p.step(); time.sleep(0.01)
    stub.close()
    assert p.interval_ms == p.base_interval                                   # no hint: back to the normal interval


# ---- authentication failures: no unauthenticated fallback ------------------------------------

@case
def authentication_failures_back_off_and_never_fall_back():
    for status in (401, 403, 429):
        stub = Stub({("POST", "/device/sync"): (status, {"error": "unauthorized"}),
                     ("GET", "/notify/next"): (204, b"")})
        p, _ = make_sync(stub)
        p.next_at = notify.ticks_ms()
        end = time.monotonic() + 1.0
        while time.monotonic() < end and p.failures == 0:
            p.step(); time.sleep(0.01)
        wait = notify.ticks_diff(p.next_at, notify.ticks_ms())
        # force the next attempts to happen now: it must keep trying to authenticate, not go legacy
        for _ in range(3):
            p.next_at = notify.ticks_ms()
            for _ in range(60):
                p.step(); time.sleep(0.005)
        stub.close()
        assert wait >= device.AUTH_BACKOFF_MS - 200, (status, wait)
        assert p.mode == "sync" and p.auth_failures >= 2, (status, p.mode, p.auth_failures)
        assert all(path == ("POST", "/device/sync") for path in stub.paths()), (status, stub.paths())


@case
def other_failures_do_not_change_the_mode_either():
    for behaviour in ((500, b"oops"), (503, b""), (200, b"not json"), (200, {"store": 1}), ("hang", b"")):
        stub = Stub({("POST", "/device/sync"): behaviour, ("GET", "/notify/next"): (204, b"")})
        p, _ = make_sync(stub)
        p.timeout_ms = 300
        drive(p, 1.0)
        stub.close()
        assert p.mode == "sync" and p.failures >= 1, (behaviour, p.mode, p.failures)
        assert all(path[0] == "POST" for path in stub.paths()), (behaviour, stub.paths())


@case
def connection_refused_keeps_sync_mode():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    state, _ = make_state()
    p = device.DeviceSync(state, "127.0.0.1", port, interval_ms=30, key=KEY)
    drive(p, 0.6)
    assert p.mode == "sync" and p.failures >= 1


# ---- no unauthenticated fallback (docs/security.md) ------------------------------------------------

@case
def a_server_without_sync_is_a_failure_never_a_fallback_to_the_old_route():
    note = {"id": 4, "kind": "attention", "label": "proj", "age": 0}
    stub = Stub({("GET", "/notify/next"): (200, note)})                         # an old server: no /device/sync, an open /notify/next
    p, _ = make_sync(stub)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        notes, _ = drive(p, 1.0, stop_when=lambda n: True)
    stub.close()
    assert p.mode == "sync" and p.failures >= 1 and p.missing_route and notes == []
    assert all(path == ("POST", "/device/sync") for path in stub.paths()), stub.paths()
    assert all(r["headers"].get("x-device-key") == KEY for r in stub.requests)
    assert out.getvalue().count("update the server") == 1                     # said once, not on every retry


@case
def after_a_missing_route_it_keeps_the_protocol_and_recovers_when_the_server_has_it():
    original = device.MISSING_ROUTE_BACKOFF_MS
    device.MISSING_ROUTE_BACKOFF_MS = 200
    try:
        stub = Stub({})
        p, state = make_sync(stub)
        with contextlib.redirect_stdout(io.StringIO()):
            drive(p, 0.4)
        assert p.missing_route
        stub.responses[("POST", "/device/sync")] = sync_ok(store="S5", rev=2, settings={"brightness": 33})   # server upgraded
        with contextlib.redirect_stdout(io.StringIO()):
            drive(p, 1.5)
        stub.close()
        assert not p.missing_route and (state.store, state.rev) == ("S5", 2) and state.lcd.calls[-1] == device.pct_to_raw(33)
        assert all(path == ("POST", "/device/sync") for path in stub.paths())
    finally:
        device.MISSING_ROUTE_BACKOFF_MS = original


@case
def without_a_usable_key_the_stick_sends_nothing_at_all():
    note = {"id": 4, "kind": "attention", "label": "proj", "age": 0}
    for key_text in ("", "short", "bad key with spaces " * 3, "x" * 40 + "\r\nX-Evil: 1", "é" * 40):
        stub = Stub({("GET", "/notify/next"): (200, note), ("POST", "/device/sync"): sync_ok()})
        state, _ = make_state()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            p = device.DeviceSync(state, "127.0.0.1", stub.port, interval_ms=30, read=lambda path, t=key_text: t)
            notes, _ = drive(p, 1.0, stop_when=lambda n: True)
        stub.close()
        assert p.mode == "nokey" and notes == [] and stub.paths() == [], (key_text, stub.paths())
        assert "Device key missing or invalid" in out.getvalue() and "nothing is sent" in out.getvalue()
        if key_text.strip():
            assert key_text.strip() not in out.getvalue()                       # the bad key text is never echoed


@case
def key_is_read_from_the_key_file_and_never_printed():
    stub = Stub({("POST", "/device/sync"): sync_ok(), ("GET", "/notify/next"): (204, b"")})
    state, _ = make_state()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        p = device.DeviceSync(state, "127.0.0.1", stub.port, interval_ms=30, read=lambda path: KEY + "\n")
        drive(p, 0.6)
    stub.close()
    assert p.mode == "sync" and stub.requests[0]["headers"]["x-device-key"] == KEY
    assert KEY not in out.getvalue()


# ---- transport details -----------------------------------------------------------------------

@case
def a_request_sent_in_small_pieces_still_arrives_whole():
    class Trickle:
        def __init__(self, sock):
            self.sock = sock

        def send(self, data):
            return self.sock.send(data[:7])

        def __getattr__(self, name):
            return getattr(self.sock, name)

    stub = Stub({("POST", "/device/sync"): sync_ok(store="S1", rev=1, settings={"brightness": 80})})
    state, _ = make_state()

    class Slow(device.DeviceSync):
        def _start(self, now):
            device.DeviceSync._start(self, now)
            if self.sock is not None:
                self.sock = Trickle(self.sock)

    p = Slow(state, "127.0.0.1", stub.port, interval_ms=40, key=KEY)
    # register the wrapped socket's fd with the poller as the real one
    drive(p, 2.0, stop_when=lambda n: False)
    stub.close()
    r = stub.requests[0]
    assert int(r["headers"]["content-length"]) == len(r["body"]) and json.loads(r["body"])["proto"] == 1
    assert state.lcd.calls and state.lcd.calls[-1] == device.pct_to_raw(80)


@case
def step_never_blocks_during_a_post_to_a_hanging_server():
    stub = Stub({("POST", "/device/sync"): ("hang", b"")})
    p, _ = make_sync(stub)
    p.timeout_ms = 400
    _, slowest = drive(p, 1.2)
    stub.close()
    assert slowest < 25, slowest
    assert p.failures >= 1                                                      # it timed out and counted a failure


@case
def abort_drops_a_post_in_flight_without_counting_a_failure():
    stub = Stub({("POST", "/device/sync"): ("hang", b"")})
    p, _ = make_sync(stub, interval_ms=500)
    p.timeout_ms = 5000
    p.next_at = notify.ticks_ms()
    for _ in range(30):
        p.step(); time.sleep(0.01)
    assert p.state != notify.IDLE
    p.abort()
    stub.close()
    assert p.state == notify.IDLE and p.sock is None and p.failures == 0


@case
def cache_saving_happens_inside_step_when_idle_only():
    stub = Stub({("POST", "/device/sync"): sync_ok(store="S1", rev=2, settings={"brightness": 66})})
    files = Files()
    state, _ = make_state(files)
    p = device.DeviceSync(state, "127.0.0.1", stub.port, interval_ms=40, key=KEY)
    drive(p, 1.0)
    stub.close()
    assert files.writes and json.loads(files.writes[0][1]) == {
        "store": "S1", "rev": 2, "brightness": 66, "idle_timeout_s": 0, "sleep_mode": "dim"}


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__, flush=True)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:300], flush=True)
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:300], flush=True)
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
