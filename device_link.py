"""Server side of the Stick's authenticated sync, and the status the panel shows.

The Stick only reports while it is idle, so silence is NOT proof of a lost
connection: contact is classified purely by the age of the last authenticated
report (see classify_contact), and "confirmed" means the latest report carries the
same store id, revision and brightness as the desired state. Design notes:
docs/desktop-panel.md.
"""
import hmac
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from reminders import DEVICE_RESULTS, DEVICE_STAGES, ReminderError, ReminderService
from settings_store import BRIGHTNESS_MAX, BRIGHTNESS_MIN, COMMAND_RESULTS, SETTINGS, Store

FRESH_S = 10                 # a report this recent is "fresh"
GAP_S = 300                  # up to this is an expected gap; beyond it the information is stale
POLL_NORMAL_MS = 3000
POLL_FAST_MS = 1000
FAST_AFTER_CHANGE_S = 20     # poll fast this long after a panel change...
FAST_WHILE_PENDING_S = 60    # ...and while a change is unconfirmed, up to this long
PENDING_NOTE_S = 30          # the panel flags a change that is still unconfirmed after this long
PERSIST_EVERY_S = 30         # the last report is written to the database at most this often
MAX_BODY = 1024
AUTH_FAILS_PER_MIN = 20
MIN_KEY_CHARS = 32
# One-shot sleep/wake commands expire; the environment variable exists for tests and tuning.
COMMAND_TTL_S = float(os.environ.get("STICK_COMMAND_TTL_S", 45))
SCREEN_STATES = ("awake", "face", "dim", "off")
DEFAULT_CAPS = ["brightness"]        # what a Stick that reports no capabilities (v0.7.0) can do


# ---- the device key: the one authentication check for every LAN route ---------------------------------------------------------

class KeyGate:
    """The device key (read once, at start-up) and the per-client lock-out after repeated failures.

    Shared by every route a Stick may call (audio upload, job status, reply download, sync, reminders), so a client that keeps
    guessing is locked out of all of them at once. The key is never logged and never returned; failures log only the client address."""

    def __init__(self, path, clock=time.time):
        self._clock = clock
        self._key = self._load_key(Path(path))
        self._failures = defaultdict(deque)
        self._last_log = {}
        self._lock = threading.Lock()

    @staticmethod
    def _load_key(path):
        try:
            raw = path.read_text().strip()
        except OSError:
            return None
        if len(raw) < MIN_KEY_CHARS:
            print("[panel] device key file is too short; ignoring it", flush=True)
            return None
        if path.stat().st_mode & 0o077:
            print("[panel] warning: the device key file is readable by other users (run chmod 600)", flush=True)
        return raw.encode()

    @property
    def provisioned(self):
        return self._key is not None

    def authentic(self, presented):
        return bool(self._key and presented and hmac.compare_digest(presented.encode(), self._key))

    def _prune(self, window, now):
        while window and now - window[0] > 60:
            window.popleft()

    def failed(self, client_ip):
        now = self._clock()
        with self._lock:
            window = self._failures[client_ip]
            window.append(now)
            self._prune(window, now)
            log = now - self._last_log.get(client_ip, 0) > 10
            if log:
                self._last_log[client_ip] = now
        if log:
            print(f"[device] authentication failed from {client_ip}", flush=True)

    def locked_out(self, client_ip):
        now = self._clock()
        with self._lock:
            window = self._failures[client_ip]
            self._prune(window, now)
            return len(window) >= AUTH_FAILS_PER_MIN

    def check(self, presented_key, client_ip):
        """None when the request may proceed, else the (status, body) to answer. Called before any body is read."""
        if self.locked_out(client_ip):
            return 429, {"error": "too many failed attempts"}
        if not self.authentic(presented_key):
            self.failed(client_ip)
            return 401, {"error": "unauthorized"}
        return None


# ---- pure status rules ------------------------------------------------------------------

def classify_contact(age):
    """never / fresh / gap / stale from the age in seconds of the last authenticated report."""
    if age is None:
        return "never"
    if age <= FRESH_S:
        return "fresh"
    return "gap" if age <= GAP_S else "stale"


def setting_status(key, store_id, rev, settings, report):
    """unset / pending / confirmed / differs for one desired setting against the last report."""
    desired = settings.get(key)
    if desired is None:
        return "unset"
    if report is None:
        return "pending"
    same_state = report.get("store") == store_id and report.get("rev") == rev
    if same_state and report.get(key) == desired:
        return "confirmed"
    return "differs" if same_state else "pending"


def brightness_status(store_id, rev, settings, report):
    return setting_status("brightness", store_id, rev, settings, report)


def _text(value, limit):
    return re.sub(r"[^\x20-\x7e]", "", str(value or ""))[:limit]


def _ident(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2 ** 62:
        raise ValueError("bad id")
    return value


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("number out of range")
    return value


def clean_report(body):
    """Validate a sync body and keep only the known fields. Raises ValueError."""
    if not isinstance(body, dict) or body.get("proto") != 1:
        raise ValueError("unsupported protocol")
    report = {
        "store": _text(body.get("store"), 64),
        "boot": _text(body.get("boot"), 32),
        "app": _text(body.get("app"), 32),
        "fw": _text(body.get("fw"), 32),
        "rev": _number(body.get("rev", 0), 0, 2 ** 31),
        "notif": _number(body.get("notif", 0), 0, 2 ** 62),
    }
    for key, low, high in (("brightness", 0, 100), ("battery", 0, 100), ("rssi", -130, 0), ("uptime", 0, 2 ** 31),
                           ("voice_volume", 0, 100), ("chime_volume", 0, 100), ("idle_timeout_s", 0, 3600)):
        if body.get(key) is not None:
            report[key] = _number(body[key], low, high)
    if isinstance(body.get("charging"), bool):
        report["charging"] = body["charging"]
    if body.get("sleep_mode") in ("dim", "off"):
        report["sleep_mode"] = body["sleep_mode"]
    if body.get("screen") in SCREEN_STATES:
        report["screen"] = body["screen"]
    if isinstance(body.get("caps"), list):
        report["caps"] = [_text(c, 16) for c in body["caps"][:8] if isinstance(c, str) and _text(c, 16)]
    if isinstance(body.get("cmds"), list):
        results = []
        for item in body["cmds"][:8]:
            # one garbled acknowledgement is dropped on its own; it must never block the whole sync
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] in COMMAND_RESULTS:
                try:
                    results.append([_number(item[0], 0, 2 ** 62), item[1]])
                except ValueError:
                    continue
        report["cmds"] = results
    if isinstance(body.get("rems"), list):
        rems = []
        for item in body["rems"][:8]:                      # reminder results the Stick re-reports until the server has them
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] in DEVICE_RESULTS:
                try:
                    rems.append([_number(item[0], 0, 2 ** 62), item[1]])
                except ValueError:
                    continue
        report["rems"] = rems
    return report


class DeviceLink:
    def __init__(self, next_notification, busy_hint=lambda: False, data_dir=None, secrets_dir=None,
                 clock=time.time, synth=None, gate=None):
        base = Path(__file__).resolve().parent
        data_dir = Path(os.environ.get("STICK_DATA_DIR") or data_dir or base / "data")
        secrets_dir = Path(os.environ.get("STICK_SECRETS_DIR") or secrets_dir or base / "secrets")
        self._clock = clock
        self._next_notification = next_notification
        self._busy_hint = busy_hint
        self.store = Store(data_dir / "panel.db")
        self.reminders = ReminderService(self.store, data_dir / "reminders", clock, synth, busy_hint)
        self.gate = gate or KeyGate(secrets_dir / "device_key", clock)
        self._lock = threading.Lock()
        self._fast_until = 0.0
        saved = self.store.load_report()
        self._received, self._report = saved if saved else (None, None)
        self._persisted_at = self._received or 0.0
        self._persisted_report = self._report

    # ---- device key (never logged, never returned): see KeyGate -------------------------
    @property
    def key_provisioned(self):
        return self.gate.provisioned

    def _gate(self, presented_key, client_ip):
        return self.gate.check(presented_key, client_ip)

    # ---- POST /device/sync -----------------------------------------------------------------
    def sync(self, presented_key, raw_body, client_ip):
        """Handle one sync request. Returns (http_status, dict). raw_body is the request bytes.
        The key is checked before the body is even parsed."""
        refused = self._gate(presented_key, client_ip)
        if refused:
            return refused
        try:
            report = clean_report(json.loads(raw_body))
        except (ValueError, TypeError):
            return 400, {"error": "bad request"}

        now = self._clock()
        with self._lock:
            self._report, self._received = report, now
            self._persist_if_due(report, now)
        for command_id, result in report.get("cmds", []):        # what the Stick says it actually did
            self.store.finish_command(command_id, result, now)
        self.store.fail_other_boot(report["boot"], now)          # never replay commands lost with an old boot
        commands = []
        caps = report.get("caps", DEFAULT_CAPS)
        command = self.store.next_command(now) if "sleep" in caps else None
        if command and (command["status"] == "pending" or command["delivered_boot"] == report["boot"]):
            commands.append({"id": command["id"], "type": command["type"],
                             "ttl_ms": max(1, int((command["expires_at"] - now) * 1000))})
            self.store.mark_delivered(command["id"], now, report["boot"])
        for occ, result in report.get("rems", []):                # what the Stick says happened to a reminder
            self.reminders.device_state(occ, result, report["boot"], now)
        self.reminders.reconcile_boot(report["boot"], now)        # one bounded redelivery after a restart, never a silent "done"
        notification = self._next_notification(report["notif"], client_ip)
        reminder = None
        if notification is None and "reminder" in caps:            # a reminder never shares a reply with a notification
            reminder = self.reminders.next_for_device(now, report["boot"])
        store_id, rev, settings, _ = self.store.desired()
        times = self.store.setting_times()
        pending = any(setting_status(k, store_id, rev, settings, report) != "confirmed"
                      and now - times.get(k, 0) <= FAST_WHILE_PENDING_S for k in settings)
        fast = now < self._fast_until or pending or bool(commands) or reminder is not None
        return 200, {
            "store": store_id, "rev": rev, "settings": settings, "commands": commands,
            "notification": notification, "reminder": reminder,
            "poll_ms": POLL_FAST_MS if fast else POLL_NORMAL_MS,
        }

    # ---- reminder endpoints used only by the Stick (same device key) --------------------------------------
    def reminder_state(self, presented_key, raw_body, client_ip):
        """POST /device/reminder/state {occ, state, boot}: an immediate stage or result; the sync repeats results later."""
        refused = self._gate(presented_key, client_ip)
        if refused:
            return refused
        try:
            body = json.loads(raw_body)
            occ, state, boot = _number(body["occ"], 0, 2 ** 62), body["state"], _text(body["boot"], 32)
            if state not in DEVICE_STAGES and state not in DEVICE_RESULTS:
                raise ValueError("state")
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "bad request"}
        return 200, {"changed": self.reminders.device_state(occ, state, boot, self._clock())}

    def reminder_media(self, presented_key, occ_text, client_ip):
        """GET /device/reminder.wav?occ=N: (200, path) for the audio of the CURRENT delivery, else (404, ...)."""
        refused = self._gate(presented_key, client_ip)
        if refused:
            return refused
        try:
            occ = _number(int(occ_text), 0, 2 ** 62)
        except (ValueError, TypeError):
            return 400, {"error": "bad request"}
        path = self.reminders.media_for(occ, self._clock())
        return (200, path) if path else (404, {"error": "not available"})

    def _persist_if_due(self, report, now):
        volatile = ("uptime", "rssi", "battery")
        steady = {k: v for k, v in report.items() if k not in volatile}
        previous = {k: v for k, v in (self._persisted_report or {}).items() if k not in volatile}
        if steady != previous or now - self._persisted_at >= PERSIST_EVERY_S:
            self.store.save_report(report, now)
            self._persisted_report, self._persisted_at = report, now

    # ---- panel side ------------------------------------------------------------------------
    def set_setting(self, key, value):
        """Store a desired setting. Raises ValueError for an unknown key or an invalid value."""
        _, changed = self.store.set_setting(key, value, self._clock())
        if changed:
            self._fast_until = self._clock() + FAST_AFTER_CHANGE_S
        return self.snapshot()

    def set_brightness(self, value):
        return self.set_setting("brightness", value)

    def command(self, kind):
        """Queue a one-shot sleep/wake command that expires after COMMAND_TTL_S. Raises ValueError."""
        self.store.add_command(kind, COMMAND_TTL_S, self._clock())
        self._fast_until = self._clock() + FAST_AFTER_CHANGE_S
        return self.snapshot()

    def reminder(self, action, data):
        """Panel actions on reminders. Raises ReminderError (fixable by the user) or ValueError/TypeError (malformed)."""
        service = self.reminders
        fold = data.get("fold")
        if action == "preview":
            return {"preview": service.preview(data.get("when"), fold)}
        if action == "reminders":                                  # POST /api/reminders = create
            service.create(data.get("text"), data.get("mode"), data.get("when"), fold, data.get("client_key"))
        elif action == "edit":
            service.edit(_ident(data.get("id")), _ident(data.get("occ")), data.get("text"), data.get("mode"),
                         data.get("when"), fold)
        elif action == "cancel":
            service.cancel(_ident(data.get("id")), _ident(data.get("occ")))
        else:
            raise ValueError("unknown action")
        self._fast_until = self._clock() + FAST_AFTER_CHANGE_S
        return self.snapshot()

    def snapshot(self):
        now = self._clock()
        with self._lock:
            report, received = self._report, self._received
        store_id, rev, settings, updated = self.store.desired()
        times = self.store.setting_times()
        age = None if received is None else max(0.0, now - received)
        contact = classify_contact(age)
        state = brightness_status(store_id, rev, settings, report)
        controls = {}
        for key in SETTINGS:
            key_state = setting_status(key, store_id, rev, settings, report)
            controls[key] = {
                "desired": settings.get(key),
                "reported": None if report is None else report.get(key),
                "state": key_state,
                "pending_s": (now - times[key]) if key in times and key_state in ("pending", "differs") else None,
            }
        command = self.store.latest_command(now)
        command_view = None
        if command:
            open_now = command["status"] in ("pending", "delivered")
            command_view = {
                "id": command["id"], "type": command["type"],
                "state": {"pending": "queued", "delivered": "sent"}.get(command["status"], command["status"]),
                "age_s": now - command["created_at"],
                "ttl_left_s": max(0.0, command["expires_at"] - now) if open_now else None,
                "detail": command["detail"],
            }
        return {
            "server_time": now,
            "key_provisioned": self.key_provisioned,
            "device": {
                "contact": contact,
                "age_s": age,
                "report": report,
                # Context only, never evidence of connection: reports pause while the Stick is busy.
                "voice_job_active": bool(self._busy_hint()) if contact == "gap" else False,
            },
            "brightness": {
                "min": BRIGHTNESS_MIN, "max": BRIGHTNESS_MAX,
                "desired": settings.get("brightness"),
                "reported": None if report is None else report.get("brightness"),
                "state": state,
                "rev": rev, "store": store_id,
                "pending_s": (now - updated) if updated is not None and state in ("pending", "differs") else None,
                "pending_note_s": PENDING_NOTE_S,
            },
            "settings": controls,
            # None until the first report; a Stick that reports no capabilities is a v0.7.0 one (brightness only).
            "caps": None if report is None else report.get("caps", DEFAULT_CAPS),
            "screen": None if report is None else report.get("screen"),
            "command": command_view,
            "reminders": self.reminders.view(now),
        }
