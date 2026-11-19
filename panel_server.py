"""Loopback-only web panel: serves the Hebrew UI in panel/ and a tiny JSON API.

Security (see docs/desktop-panel.md): the listener binds 127.0.0.1 only; every request
checks the Host header (DNS rebinding); every POST also needs a matching Origin,
Content-Type application/json and the per-process CSRF token. No cookies, no CORS.
POST /api/brightness (kept from v0.7.0), /api/setting {key, value} and /api/command {type: sleep|wake}.
Reminders (v0.9.0): POST /api/reminders/preview, /api/reminders, /api/reminders/edit, /api/reminders/cancel.
POST /hook/notify: the Claude Code notification hook on this Mac (hooks/stick_notify.py). It is not reachable from the LAN (this listener
is loopback-only), and a web page cannot send it: it needs no Origin header, Content-Type application/json and X-Stick-Hook: 1, so a
browser would need a CORS preflight that this server never grants. Size-limited and rate-limited. No CSRF token (the hook has none).
"""
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reminders import ReminderError

HOST = "127.0.0.1"
DEFAULT_PORT = 8001
# STICK_PANEL_DIR serves the UI files from another folder (a relative path is taken from the project folder). It exists to try a new UI on an
# isolated test server and for a one-line frontend rollback; unset, the folder is panel/ exactly as before.
_HERE = Path(__file__).resolve().parent
PANEL_DIR = (_HERE / os.path.expanduser(os.environ["STICK_PANEL_DIR"])) if os.environ.get("STICK_PANEL_DIR") else _HERE / "panel"
MAX_BODY = 256
MAX_REMINDER_BODY = 4096          # a 200-character Hebrew text escaped as JSON is about 1.3 KB
REMINDER_PATHS = ("/api/reminders/preview", "/api/reminders", "/api/reminders/edit", "/api/reminders/cancel")
RATE_PER_S = 8.0
BURST = 10
MAX_HOOK_BODY = 2048
HOOK_RATE_PER_S = 2.0
HOOK_BURST = 20

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/reminders.js": ("reminders.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class RateLimiter:
    """Token bucket: at most RATE_PER_S sustained, BURST at once."""
    def __init__(self, rate=RATE_PER_S, burst=BURST, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens, self.last = float(burst), clock()
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "VoicePanel"
    sys_version = ""

    # ---- helpers ------------------------------------------------------------------------
    def log_message(self, format, *args):
        if not self.path.startswith("/api/status"):
            super().log_message(format, *args)

    def _headers(self, content_type, length, extra=()):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()

    def _json(self, status, data, extra=()):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._headers("application/json", len(body), extra)
        self.wfile.write(body)

    def _error(self, status, message, extra=()):
        self._json(status, {"error": message}, extra)

    def _host_ok(self):
        return self.headers.get("Host", "").lower() in self.server.allowed_hosts

    def _post_origin_ok(self):
        origin = self.headers.get("Origin", "")
        if origin not in {f"http://{h}" for h in self.server.allowed_hosts}:
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    # ---- GET ----------------------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "forbidden host")
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            return self._json(200, self.server.link.snapshot())
        if path in STATIC:
            name, content_type = STATIC[path]
            try:
                body = (PANEL_DIR / name).read_bytes()
            except OSError:
                return self._error(404, "missing panel file")
            if name == "index.html":
                body = body.replace(b"__CSRF__", self.server.token.encode())
            self.send_response(200)
            self._headers(content_type, len(body))
            self.wfile.write(body)
            return
        self._error(404, "not found")

    # ---- POST ---------------------------------------------------------------------------
    def do_POST(self):
        if not self._host_ok():
            return self._error(403, "forbidden host")
        if self.path == "/hook/notify":
            return self._hook_notify()
        if self.path not in ("/api/brightness", "/api/setting", "/api/command") + REMINDER_PATHS:
            return self._error(404, "not found")
        if not self._post_origin_ok():
            return self._error(403, "forbidden origin")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._error(415, "JSON required")
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), self.server.token.encode()):
            return self._error(403, "bad token")
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(411, "length required")
        if not 0 < size <= (MAX_REMINDER_BODY if self.path in REMINDER_PATHS else MAX_BODY):
            return self._error(413, "bad size")
        try:
            data = json.loads(self.rfile.read(size))
            if self.path in REMINDER_PATHS:
                if not isinstance(data, dict):
                    raise TypeError("object required")
                action = ("reminder", self.path.rsplit("/", 1)[1], data)
            elif self.path == "/api/brightness":
                action = ("setting", "brightness", data["value"])
            elif self.path == "/api/setting":
                action = ("setting", data["key"], data["value"])
            else:
                action = ("command", data["type"], None)
        except (ValueError, KeyError, TypeError):
            return self._error(400, "bad request")
        if not self.server.limiter.take():
            return self._error(429, "too many requests", [("Retry-After", "1")])
        try:
            if action[0] == "reminder":
                snapshot = self.server.link.reminder(action[1], action[2])
            elif action[0] == "setting":
                snapshot = self.server.link.set_setting(action[1], action[2])
            else:
                snapshot = self.server.link.command(action[1])
        except ReminderError as error:
            return self._json(error.status, {"error": str(error), "code": error.code, "options": error.options})
        except (ValueError, TypeError) as error:
            return self._error(400, str(error))
        self._json(200, snapshot)


    def _hook_notify(self):
        """A notification from the Claude Code hook on this Mac: only {kind, label, session, prompt}; nothing is returned but the outcome."""
        if self.server.notify is None:
            return self._error(404, "not found")
        if "Origin" in self.headers or self.headers.get("X-Stick-Hook") != "1":
            return self._error(403, "hook only")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._error(415, "JSON required")
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(411, "length required")
        if not 0 < size <= MAX_HOOK_BODY:
            return self._error(413, "bad size")
        if not self.server.hook_limiter.take():
            return self._error(429, "too many requests", [("Retry-After", "1")])
        try:
            event = json.loads(self.rfile.read(size))
            kind = event["kind"]
            if not isinstance(kind, str):
                raise TypeError("kind")
        except (ValueError, KeyError, TypeError):
            return self._error(400, "bad notification")
        accepted, reason = self.server.notify(kind, event.get("label"), event.get("session"), event.get("prompt"))
        self._json(200, {"queued": accepted, "reason": reason})


def start(link, port=DEFAULT_PORT, notify=None):
    """Start the panel listener in a daemon thread. Returns the server (read .server_address)."""
    server = ThreadingHTTPServer((HOST, port), PanelHandler)
    server.daemon_threads = True
    server.block_on_close = False
    real_port = server.server_address[1]
    server.link = link
    server.token = secrets.token_urlsafe(24)
    server.allowed_hosts = {f"127.0.0.1:{real_port}", f"localhost:{real_port}"}
    server.limiter = RateLimiter()
    server.notify = notify                       # accept_notification(kind, label, session, prompt) -> (accepted, reason), or None
    server.hook_limiter = RateLimiter(rate=HOOK_RATE_PER_S, burst=HOOK_BURST)
    threading.Thread(target=server.serve_forever, daemon=True, name="panel").start()
    return server
