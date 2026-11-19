# notify.py - non-blocking poller for Claude Code notifications from the Mac server.
#
# step() advances a small connect -> send -> read state machine by at most a
# few socket calls and returns immediately, so the face keeps animating and the
# buttons keep being read even when the server is slow or unreachable (unlike
# requests.get, which blocks until it finishes). Failures back off up to 30 s.
import errno
import json
import select
import socket

try:
    from time import ticks_add, ticks_diff, ticks_ms
except ImportError:  # CPython, for tests
    import time

    def ticks_ms():
        return int(time.monotonic() * 1000)

    def ticks_diff(a, b):
        return a - b

    def ticks_add(a, b):
        return a + b

INTERVAL_MS = 3000        # time between polls while the server answers
MAX_BACKOFF_MS = 30000    # time between polls while it does not
TIMEOUT_MS = 3000         # give up on one poll after this long
MAX_RESPONSE = 1024

IDLE, CONNECTING, READING = 0, 1, 2


class NotificationPoller:
    def __init__(self, host=None, port=None, interval_ms=INTERVAL_MS, timeout_ms=TIMEOUT_MS):   # no built-in server: see server_config.py
        self.host = host
        self.port = port
        self.interval_ms = interval_ms
        self.timeout_ms = timeout_ms
        self.after = 0            # highest notification id already shown (acknowledged)
        self.failures = 0
        self.state = IDLE
        self.sock = None
        self.poll = None
        self.buf = b""
        self.out = None           # the request being sent (built when the connection is ready)
        self.sent = 0
        self.started = 0
        self.addr = None
        self.next_at = ticks_ms()

    def ack(self, notification_id):
        """Remember that this notification was shown; the next poll acknowledges it."""
        self.after = notification_id

    def abort(self):
        """An interaction is starting: drop any request in flight and wait before the next."""
        self._close()
        self.next_at = ticks_add(ticks_ms(), self.interval_ms)

    def step(self):
        """Advance the poll without blocking. Returns a notification dict when one arrived."""
        now = ticks_ms()
        if self.state == IDLE:
            if ticks_diff(now, self.next_at) >= 0:
                self._start(now)
            return None
        if ticks_diff(now, self.started) > self.timeout_ms:
            return self._fail(now)
        try:
            events = self.poll.poll(0)
        except OSError:
            return self._fail(now)
        for _, flags in events:
            if self.state == CONNECTING:
                if flags & (select.POLLERR | select.POLLHUP):
                    return self._fail(now)
                if flags & select.POLLOUT:
                    return self._send(now)
            elif self.state == READING and flags & (select.POLLIN | select.POLLHUP | select.POLLERR):
                return self._read(now)
        return None

    # ---- internals ----------------------------------------------------------------------
    def _start(self, now):
        if self.host is None or self.port is None:       # never guess an address: without one nothing is sent
            self._fail(now)
            return
        try:
            if self.addr is None:
                self.addr = socket.getaddrinfo(self.host, self.port)[0][-1]
            sock = socket.socket()
            sock.setblocking(False)
            try:
                sock.connect(self.addr)
            except OSError as error:
                if not error.args or error.args[0] != errno.EINPROGRESS:
                    sock.close()
                    raise
            self.sock = sock
            self.poll = select.poll()
            self.poll.register(sock, select.POLLOUT)
            self.buf = b""
            self.out = None
            self.sent = 0
            self.started = now
            self.state = CONNECTING
        except OSError:
            self._fail(now)

    def _build_request(self):
        """The bytes to send. Subclasses override this (and _handle) to talk to other endpoints."""
        return ("GET /notify/next?after=%d HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n"
                % (self.after, self.host)).encode()

    def _send(self, now):
        try:
            if self.out is None:
                self.out = self._build_request()
                self.sent = 0
            written = self.sock.send(self.out[self.sent:])
        except OSError as error:
            if error.args and error.args[0] == errno.EAGAIN:
                return None
            return self._fail(now)
        self.sent += written or 0
        if self.sent < len(self.out):
            return None               # the socket stays writable: the rest goes out on a later step
        try:
            self.poll.modify(self.sock, select.POLLIN)
        except OSError:
            return self._fail(now)
        self.state = READING
        return None

    def _read(self, now):
        try:
            chunk = self.sock.recv(512)
        except OSError as error:
            if error.args and error.args[0] == errno.EAGAIN:
                return None
            return self._fail(now)
        if chunk:
            self.buf += chunk
            if len(self.buf) > MAX_RESPONSE:
                return self._fail(now)
            return None
        return self._finish(now)      # the server closed the connection: response complete

    def _finish(self, now):
        head, sep, body = self.buf.partition(b"\r\n\r\n")
        try:
            status = int(head.split(b" ", 2)[1])
        except (IndexError, ValueError):
            return self._fail(now)
        if not sep:
            return self._fail(now)
        return self._handle(status, body, now)

    def _handle(self, status, body, now):
        """Interpret a complete response. Returns a notification dict or None."""
        if status == 204:
            return self._ok(now)
        if status != 200:
            return self._fail(now)
        try:
            note = json.loads(body)
            note_id = note["id"]
        except (ValueError, KeyError, TypeError):
            return self._fail(now)
        self._ok(now)
        return note if note_id > self.after else None

    def _ok(self, now):
        self._close()
        self.failures = 0
        self.next_at = ticks_add(now, self.interval_ms)

    def _fail(self, now, at_least_ms=0):
        self._close()
        self.failures += 1
        delay = self.interval_ms << min(self.failures, 4)
        if delay > MAX_BACKOFF_MS:
            delay = MAX_BACKOFF_MS
        if delay < at_least_ms:
            delay = at_least_ms
        self.next_at = ticks_add(now, delay)
        return None

    def _close(self):
        if self.sock is not None:
            try:
                self.poll.unregister(self.sock)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.poll = None
        self.out = None
        self.state = IDLE
