"""Tests for stick/notify.py (the Stick's non-blocking poller) on CPython against a
real local HTTP server. Run: python3 tests/test_notify_poller.py

The same module runs on the Stick; there it is exercised against the real
server by the device checks.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stick"))
import notify  # noqa: E402


class Server:
    """HTTP/1.0 server like the real one; behaviour picks the response."""
    def __init__(self, behaviour="204", note=None):
        self.requests = []
        self.behaviour = behaviour
        self.note = note
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                b = outer.behaviour
                if b == "hang":
                    time.sleep(5)
                    return
                if b == "204":
                    self.send_response(204)
                    self.end_headers()
                elif b == "200":
                    body = json.dumps(outer.note).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif b == "500":
                    self.send_error(500)
                elif b == "garbage":
                    self.wfile.write(b"this is not http at all")
                elif b == "big":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"x" * 5000)
                elif b == "slow":
                    body = json.dumps(outer.note).encode()
                    self.wfile.write(b"HTTP/1.0 200 OK\r\n")
                    self.wfile.flush(); time.sleep(0.15)
                    self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(body))
                    self.wfile.flush(); time.sleep(0.15)
                    self.wfile.write(body)

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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def drive(poller, seconds, stop_when=None):
    """Call step() like the Stick's loop (every ~20 ms); return (results, slowest_step_ms)."""
    results, slowest = [], 0.0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        t = time.monotonic()
        r = poller.step()
        slowest = max(slowest, (time.monotonic() - t) * 1000)
        if r is not None:
            results.append(r)
            if stop_when and stop_when(r):
                break
        time.sleep(0.02)
    return results, slowest


NOTE = {"id": 1700000000001, "kind": "turn_finished", "label": "my-app", "age": 0}
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def nothing_pending_returns_nothing():
    s = Server("204")
    p = notify.NotificationPoller("127.0.0.1", s.port, interval_ms=60)
    results, _ = drive(p, 0.6)
    s.close()
    assert results == [] and len(s.requests) >= 3 and p.failures == 0, (results, s.requests)


@case
def delivers_a_notification_and_acknowledges_it():
    s = Server("200", NOTE)
    p = notify.NotificationPoller("127.0.0.1", s.port, interval_ms=60)
    results, _ = drive(p, 2, stop_when=lambda r: True)
    assert results == [NOTE], results
    assert s.requests[0] == "/notify/next?after=0"
    p.ack(NOTE["id"])
    drive(p, 0.4)
    s.close()
    assert s.requests[-1] == f"/notify/next?after={NOTE['id']}", s.requests
    # the server repeating an already-acknowledged id must not be shown again
    results2, _ = drive(p, 0.3)
    assert results2 == []


@case
def step_never_blocks_even_against_a_hanging_server():
    s = Server("hang")
    p = notify.NotificationPoller("127.0.0.1", s.port, interval_ms=50, timeout_ms=400)
    results, slowest = drive(p, 1.2)
    s.close()
    assert results == [] and slowest < 25, slowest
    assert p.failures >= 1                    # it timed out and counted a failure


@case
def step_never_blocks_when_nothing_listens():
    p = notify.NotificationPoller("127.0.0.1", free_port(), interval_ms=40)
    results, slowest = drive(p, 1.0)
    assert results == [] and slowest < 25, slowest
    assert p.failures >= 1


@case
def failures_back_off_and_recover():
    port = free_port()
    p = notify.NotificationPoller("127.0.0.1", port, interval_ms=100)
    delays = []
    last_failures = 0
    end = time.monotonic() + 4
    while time.monotonic() < end and len(delays) < 4:
        p.step()
        if p.failures != last_failures:
            last_failures = p.failures
            delays.append(notify.ticks_diff(p.next_at, notify.ticks_ms()))
        time.sleep(0.01)
    assert len(delays) >= 3 and delays == sorted(delays) and delays[-1] > delays[0] * 1.5, delays
    assert max(delays) <= notify.MAX_BACKOFF_MS
    # the server comes up on that port: the poller recovers and resets its backoff
    s = Server.__new__(Server)
    s.requests, s.behaviour, s.note = [], "204", None

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            s.requests.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", port), H)
    http.daemon_threads = True
    http.block_on_close = False
    threading.Thread(target=http.serve_forever, daemon=True).start()
    p.next_at = notify.ticks_ms()
    drive(p, 0.8)
    http.shutdown(); http.server_close()
    assert p.failures == 0 and s.requests, (p.failures, s.requests)


@case
def abort_drops_the_request_in_flight():
    s = Server("hang")
    p = notify.NotificationPoller("127.0.0.1", s.port, interval_ms=500, timeout_ms=5000)
    p.next_at = notify.ticks_ms()
    for _ in range(20):
        p.step()
        time.sleep(0.01)
    assert p.state != notify.IDLE and p.sock is not None
    p.abort()
    wait_left = notify.ticks_diff(p.next_at, notify.ticks_ms())
    s.close()
    assert p.state == notify.IDLE and p.sock is None and p.failures == 0
    assert wait_left > 300, wait_left      # waits before polling again


@case
def bad_responses_count_as_failures_without_raising():
    for behaviour in ("500", "garbage", "big"):
        s = Server(behaviour)
        p = notify.NotificationPoller("127.0.0.1", s.port, interval_ms=40)
        results, slowest = drive(p, 0.8)
        s.close()
        assert results == [] and p.failures >= 1 and slowest < 25, (behaviour, p.failures, slowest)


@case
def a_response_arriving_in_pieces_is_assembled():
    s = Server("slow", NOTE)
    p = notify.NotificationPoller("127.0.0.1", s.port, interval_ms=60)
    results, slowest = drive(p, 3, stop_when=lambda r: True)
    s.close()
    assert results == [NOTE] and slowest < 25, (results, slowest)


@case
def interval_is_respected():
    s = Server("204")
    p = notify.NotificationPoller("127.0.0.1", s.port, interval_ms=300)
    drive(p, 1.0)
    s.close()
    assert 2 <= len(s.requests) <= 4, len(s.requests)


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
