# sleep.py - screen-only sleep logic for the StickS3 (pure logic: no hardware, no network).
#
# The Sleeper decides WHAT should happen to the screen; the main loop performs it and then calls
# performed(), so a command counts as executed only after the action really happened. step() is
# called only while the Stick is idle (never during recording, thinking, downloading, playback,
# a shake reaction or a notification), and Wi-Fi is not needed: the idle timer and the manual
# commands work with the Mac server absent. Wi-Fi and sync keep running while the screen sleeps;
# this is NOT MCU deep sleep.
#
# States: awake -> face (the sleeping face, shown briefly) -> dim or off -> awake (on wake).
# Manual "sleep"/"wake" are one-shot commands with an expiry, never a permanent desired state: a
# local wake or a reboot cannot put the device back to sleep, and an expired command is never run.
try:
    from time import ticks_add, ticks_diff
except ImportError:  # CPython, for tests
    def ticks_diff(a, b):
        return a - b

    def ticks_add(a, b):
        return a + b

AWAKE, FACE, DIM, OFF = "awake", "face", "dim", "off"
FACE_MS = 2500                  # how long the sleeping face is shown before the backlight is lowered
TIMEOUTS = (0, 30, 60, 300)     # idle timeout in seconds; 0 = automatic sleep disabled (the default)
MODES = ("dim", "off")          # after the face: keep a dim sleeping face, or turn the backlight off
MAX_RESULTS = 4                 # recent command results kept for reporting


class Sleeper:
    def __init__(self, now_ms):
        self.timeout_s = 0
        self.mode = "dim"
        self.state = AWAKE
        self.last_activity = now_ms
        self.face_until = now_ms
        self.cmd = None             # [id, kind, deadline_ms] of the one pending command
        self.results = []           # [[id, "executed"|"expired"|"ignored"], ...] newest last
        self.seen = 0               # highest command id ever accepted: older or repeated ids are ignored

    @property
    def asleep(self):
        return self.state != AWAKE

    def configure(self, timeout_s=None, mode=None):
        if timeout_s in TIMEOUTS and not isinstance(timeout_s, bool):
            self.timeout_s = timeout_s
        if mode in MODES:
            self.mode = mode

    def activity(self, now_ms):
        """Something real happened (a press, a shake, a notification, an interaction): restart the idle timer.
        Polling, sync and panel refreshes are NOT activity and must never call this."""
        self.last_activity = now_ms

    # ---- commands --------------------------------------------------------------------------
    def submit(self, cmd_id, kind, ttl_ms, now_ms):
        """Accept a command from the server. A repeated or older id, an unknown kind or a spent ttl is ignored."""
        if kind not in ("sleep", "wake") or cmd_id <= self.seen or ttl_ms <= 0:
            return False
        if self.cmd is not None:
            self._finish(self.cmd[0], "ignored")      # replaced by a newer command before it could run
        self.seen = cmd_id
        self.cmd = [cmd_id, kind, ticks_add(now_ms, ttl_ms)]
        return True

    def _finish(self, cmd_id, result):
        self.results.append([cmd_id, result])
        del self.results[:-MAX_RESULTS]

    def _done(self, kind):
        if self.cmd is not None and self.cmd[1] == kind:
            self._finish(self.cmd[0], "executed")
            self.cmd = None

    # ---- the decision, then the report that it was carried out -------------------------------
    def step(self, now_ms):
        """The next screen action while idle: 'sleep', 'dim', 'off', 'wake' or None."""
        if self.cmd is not None:
            cmd_id, kind, deadline = self.cmd
            if ticks_diff(now_ms, deadline) >= 0:
                self._finish(cmd_id, "expired")       # never run a command late
                self.cmd = None
            elif kind == "sleep" and self.state == AWAKE:
                return "sleep"
            elif kind == "wake" and self.state != AWAKE:
                return "wake"
            else:
                self._finish(cmd_id, "executed")      # already in the requested state: nothing to do
                self.cmd = None
        if self.state == FACE and ticks_diff(now_ms, self.face_until) >= 0:
            return self.mode
        if self.state == AWAKE and self.timeout_s and ticks_diff(now_ms, self.last_activity) >= self.timeout_s * 1000:
            return "sleep"
        return None

    def performed(self, action, now_ms):
        """Call after the action was actually carried out (the face shown, the backlight changed)."""
        if action == "sleep":
            self.state = FACE
            self.face_until = ticks_add(now_ms, FACE_MS)
            self._done("sleep")
        elif action == "dim":
            self.state = DIM
        elif action == "off":
            self.state = OFF
        elif action == "wake":
            self.state = AWAKE
            self.last_activity = now_ms
            self._done("wake")

    def report_fields(self):
        return {"screen": self.state, "idle_timeout_s": self.timeout_s, "sleep_mode": self.mode,
                "cmds": [list(r) for r in self.results]}
