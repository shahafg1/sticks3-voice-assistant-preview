# device.py - the Stick's side of the desktop panel (docs/desktop-panel.md).
#
# DeviceState applies the desired brightness, reads it back, reports the device state
# and caches the last applied values. DeviceSync is the non-blocking poller from
# notify.py, speaking the authenticated POST /device/sync protocol.
#
# There is NO unauthenticated mode (docs/security.md). Without a usable key file nothing
# is sent at all; authentication failures (401/403/429) back off and keep authenticating;
# a server without /device/sync (404: too old, or its panel is off) is treated as a failure,
# never as a reason to fall back to the old unauthenticated /notify/next. The key is never printed.
import json

from notify import (INTERVAL_MS, TIMEOUT_MS, NotificationPoller, ticks_diff,
                    ticks_ms)
from reminder import Reminders
from sleep import Sleeper

APP_VERSION = "0.9.0-dev"
CAPS = ["brightness", "volume", "sleep", "reminder"]   # what this firmware can do; a v0.7.0 Stick reports none
DEFAULT_VOICE_RAW = 255       # today's effective volume of answers and shake clips (100 percent)
DEFAULT_CHIME_RAW = 70        # today's effective chime volume (about 27 percent); unset settings keep these exactly
DIM_PCT = 3                   # backlight level of the dimmed sleeping face (a tunable constant)
PROTO = 1
KEY_FILE = "/flash/device_key.txt"
CACHE_FILE = "/flash/device_cache.json"
BRIGHTNESS_MIN = 5            # percent; below this the screen would look dead
BRIGHTNESS_MAX = 100
CACHE_EVERY_MS = 60000        # write the cache file at most this often
BATTERY_EVERY_MS = 30000      # read the battery at most this often
AUTH_BACKOFF_MS = 30000       # wait at least this long after an authentication failure
MISSING_ROUTE_BACKOFF_MS = 60000   # a server without /device/sync: retry this rarely (never unauthenticated)
POLL_MIN_MS = 500
POLL_MAX_MS = 10000


def pct_to_raw(percent):
    return (percent * 255 + 50) // 100


def raw_to_pct(raw):
    return (raw * 100 + 127) // 255


def clamp_pct(percent):
    return BRIGHTNESS_MIN if percent < BRIGHTNESS_MIN else (BRIGHTNESS_MAX if percent > BRIGHTNESS_MAX else percent)


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def write_text(path, text):
    with open(path, "w") as f:
        f.write(text)


def valid_key(text):
    """A usable key is 32+ letters/digits (so it cannot break the HTTP header it is sent in)."""
    return bool(text) and len(text) >= 32 and all(("0" <= c <= "9") or ("a" <= c <= "z") or ("A" <= c <= "Z")
                                                  for c in text)


def _remove(path):
    import os
    try:
        os.remove(path)
    except OSError:
        pass


def random_id():
    try:
        import os
        return "".join("%02x" % b for b in os.urandom(4))
    except Exception:
        return "%08x" % (ticks_ms() & 0xFFFFFFFF)


class DeviceState:
    """What the Stick has applied, what it reports, and the cache file."""

    def __init__(self, lcd, power=None, wlan=None, firmware="", read=read_text, write=write_text, remove=None):
        self.lcd = lcd
        self.power = power
        self.wlan = wlan
        self.firmware = str(firmware)[:32]
        self._read, self._write = read, write
        self.boot = random_id()
        self.store = ""
        self.rev = 0
        self.dirty = False
        self.last_save = None
        self._battery = None
        self._battery_at = None
        self.sleeper = Sleeper(ticks_ms())      # always boots awake; sleep is never restored from the cache
        self.reminders = Reminders(self._read, self._write, remove or _remove)   # a leftover marker is reported as 'interrupted'
        self.voice_pct = None                   # None = unset: keep today's built-in volumes
        self.chime_pct = None
        self._applied_raw = None                # the backlight value we last wrote while awake (None = unknown)
        try:
            self.awake_pct = clamp_pct(raw_to_pct(lcd.getBrightness()))
        except Exception:
            self.awake_pct = 50
        self._load_cache()

    @staticmethod
    def _percent(value):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100

    def _load_cache(self):
        text = self._read(CACHE_FILE)
        if not text:
            return
        try:
            cache = json.loads(text)
            store, rev, percent = cache["store"], cache["rev"], cache["brightness"]
            if not (isinstance(store, str) and isinstance(rev, int) and isinstance(percent, int)):
                return
        except (ValueError, KeyError, TypeError):
            return
        self._set_brightness(percent)       # restore the last awake brightness right away
        self.store, self.rev = store, rev
        # fields added in v0.8.0; a v0.7.0 cache simply lacks them
        for key, attr in (("voice_volume", "voice_pct"), ("chime_volume", "chime_pct")):
            if self._percent(cache.get(key)):
                setattr(self, attr, cache[key])
        self.sleeper.configure(cache.get("idle_timeout_s"), cache.get("sleep_mode"))

    def _set_brightness(self, percent):
        """Remember the awake brightness; touch the backlight only while the screen is awake."""
        self.awake_pct = clamp_pct(percent)
        raw = pct_to_raw(self.awake_pct)
        if not self.sleeper.asleep and raw != self._applied_raw:      # no needless rewrite for unrelated changes
            self.lcd.setBrightness(raw)
            self._applied_raw = raw

    def apply(self, store, rev, settings):
        """Apply the server's desired settings if (store, rev) differs from what is applied (in either
        direction, so a restored older database also reconciles). Returns True if anything was applied.
        Volume changes are only stored: they take effect at the start of the next playback."""
        if store == self.store and rev == self.rev:
            return False
        percent = settings.get("brightness")
        if isinstance(percent, int) and not isinstance(percent, bool):
            self._set_brightness(percent)
        for key, attr in (("voice_volume", "voice_pct"), ("chime_volume", "chime_pct")):
            if self._percent(settings.get(key)):
                setattr(self, attr, settings[key])
        self.sleeper.configure(settings.get("idle_timeout_s"), settings.get("sleep_mode"))
        self.store, self.rev = store, rev
        self.dirty = True
        return True

    # ---- volume: read at the start of each playback ----------------------------------------------
    def voice_raw(self):
        return DEFAULT_VOICE_RAW if self.voice_pct is None else pct_to_raw(self.voice_pct)

    def chime_raw(self):
        return DEFAULT_CHIME_RAW if self.chime_pct is None else pct_to_raw(self.chime_pct)

    # ---- screen --------------------------------------------------------------------------------
    def screen_apply(self, action, now_ms):
        """Carry out a screen action ('sleep' needs no hardware: the app shows the face), then tell the Sleeper
        it really happened. The awake brightness is kept separately and restored on wake."""
        if action == "dim":
            self.lcd.setBrightness(pct_to_raw(DIM_PCT))
            self._applied_raw = None
        elif action == "off":
            self.lcd.setBrightness(0)
            self._applied_raw = None
        elif action == "wake":
            self._applied_raw = pct_to_raw(self.awake_pct)
            self.lcd.setBrightness(self._applied_raw)
        self.sleeper.performed(action, now_ms)

    def brightness_pct(self):
        if self.sleeper.asleep:
            return self.awake_pct            # the backlight is deliberately low: report the awake setting
        try:
            return raw_to_pct(self.lcd.getBrightness())
        except Exception:
            return None

    def report(self, notif_cursor, now_ms=None):
        now_ms = ticks_ms() if now_ms is None else now_ms
        report = {"proto": PROTO, "boot": self.boot, "app": APP_VERSION, "fw": self.firmware,
                  "store": self.store, "rev": self.rev, "notif": notif_cursor, "uptime": now_ms // 1000}
        percent = self.brightness_pct()          # read back (while awake), so a report is an acknowledgement
        if percent is not None:
            report["brightness"] = percent
        report["voice_volume"] = raw_to_pct(self.voice_raw())
        report["chime_volume"] = raw_to_pct(self.chime_raw())
        report.update(self.sleeper.report_fields())
        report.update(self.reminders.report_fields())
        report["caps"] = CAPS
        if self.power is not None:
            if self._battery_at is None or ticks_diff(now_ms, self._battery_at) >= BATTERY_EVERY_MS:
                try:
                    self._battery = (int(self.power.getBatteryLevel()), bool(self.power.isCharging()))
                except Exception:
                    self._battery = None
                self._battery_at = now_ms
            if self._battery is not None and 0 <= self._battery[0] <= 100:
                report["battery"], report["charging"] = self._battery
        if self.wlan is not None:
            try:
                rssi = int(self.wlan.status("rssi"))
                if -130 <= rssi <= 0:
                    report["rssi"] = rssi
            except Exception:
                pass
        return report

    def maybe_save(self, now_ms):
        """Persist the applied settings after a change, at most once per CACHE_EVERY_MS (never the sleep state)."""
        if not self.dirty:
            return
        if self.last_save is not None and ticks_diff(now_ms, self.last_save) < CACHE_EVERY_MS:
            return
        cache = {"store": self.store, "rev": self.rev, "brightness": self.awake_pct,
                 "idle_timeout_s": self.sleeper.timeout_s, "sleep_mode": self.sleeper.mode}
        if self.voice_pct is not None:
            cache["voice_volume"] = self.voice_pct
        if self.chime_pct is not None:
            cache["chime_volume"] = self.chime_pct
        try:
            self._write(CACHE_FILE, json.dumps(cache))
            self.dirty = False
            self.last_save = now_ms
        except OSError:
            self.last_save = now_ms             # try again later; never block the app


class DeviceSync(NotificationPoller):
    def __init__(self, state, host=None, port=None, interval_ms=INTERVAL_MS, timeout_ms=TIMEOUT_MS,
                 key=None, read=read_text):
        NotificationPoller.__init__(self, host, port, interval_ms, timeout_ms)
        self.dev = state                    # (the base class keeps its own connection state in self.state)
        self.base_interval = interval_ms
        text = (read(KEY_FILE) or "").strip() if key is None else key
        self.key = text if valid_key(text) else None
        self.mode = "sync" if self.key else "nokey"
        self.auth_failures = 0
        self.missing_route = False
        if not self.key:
            print("Device key missing or invalid: nothing is sent to the server (pair this Stick: tools/stick_setup.py key)")

    def step(self):
        note = NotificationPoller.step(self) if self.key else None   # no key: no request at all, never an unauthenticated one
        self.dev.maybe_save(ticks_ms())
        return note

    def _build_request(self):
        body = json.dumps(self.dev.report(self.after)).encode()
        head = ("POST /device/sync HTTP/1.0\r\nHost: %s\r\nX-Device-Key: %s\r\nContent-Type: application/json"
                "\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (self.host, self.key, len(body))).encode()
        return head + body

    def _handle(self, status, body, now):
        if status in (401, 403, 429):
            self.auth_failures += 1          # keep authenticating; never fall back to an unauthenticated mode
            return self._fail(now, AUTH_BACKOFF_MS)
        if status == 404:
            if not self.missing_route:       # an older server, or its panel is off: report it, keep the protocol
                print("Server has no /device/sync: update the server (no unauthenticated fallback)")
            self.missing_route = True
            return self._fail(now, MISSING_ROUTE_BACKOFF_MS)
        if status != 200:
            return self._fail(now)           # 5xx and the like: no mode change
        try:
            data = json.loads(body)
            store, rev, settings = data["store"], data["rev"], data["settings"]
            if not (isinstance(store, str) and isinstance(rev, int) and isinstance(settings, dict)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self._fail(now)
        self.auth_failures = 0
        self.missing_route = False
        self.dev.apply(store, rev, settings)
        for command in data.get("commands") or []:        # one-shot sleep/wake commands (v0.8.0 servers)
            if (isinstance(command, dict) and isinstance(command.get("id"), int) and isinstance(command.get("type"), str)
                    and isinstance(command.get("ttl_ms"), int)):
                self.dev.sleeper.submit(command["id"], command["type"], command["ttl_ms"], now)
        rem = data.get("reminder")                         # a one-time reminder (v0.9.0 servers); older servers send none
        if (isinstance(rem, dict) and isinstance(rem.get("occ"), int) and isinstance(rem.get("mode"), str)
                and isinstance(rem.get("ttl_ms"), int)):
            self.dev.reminders.submit(rem["occ"], rem["mode"], rem.get("label"), rem["ttl_ms"], rem.get("late_s"), now)
        hint = data.get("poll_ms")
        if isinstance(hint, int) and not isinstance(hint, bool):
            self.interval_ms = min(max(hint, POLL_MIN_MS), POLL_MAX_MS)
        else:
            self.interval_ms = self.base_interval
        note = data.get("notification")
        self._ok(now)
        if isinstance(note, dict) and isinstance(note.get("id"), int) and note["id"] > self.after:
            return note
        return None
