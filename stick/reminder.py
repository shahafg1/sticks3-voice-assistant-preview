# reminder.py - the Stick's side of one-time reminders (pure logic: no hardware, no network).
#
# The server hands over at most one reminder per sync reply while the Stick is idle. submit() keeps it until the main
# loop is idle and take()s it; finish() records what really happened, which is re-reported on every sync until the
# server has it. Delivery itself (face, chime or speech, the stop button) lives in voice_send.py.
#
# A small marker file records "presenting reminder N" while one is being shown. A reset in that window is reported
# as 'interrupted' by the next boot, so the server marks it failed instead of guessing, and does not repeat it.
# (A reset between receiving and writing the marker leaves no trace: the server then redelivers once. Documented.)
try:
    from time import ticks_add, ticks_diff
except ImportError:  # CPython, for tests
    def ticks_diff(a, b):
        return a - b

    def ticks_add(a, b):
        return a + b

MARKER = "/flash/rem_inflight.txt"
MODES = ("speak", "chime")
MAX_SEEN = 16                   # recent occurrence ids: a repeated offer of the same reminder is ignored
MAX_RESULTS = 4                 # recent results kept for re-reporting
LABEL_MAX = 24


def ascii_label(text):
    out = "".join(c for c in str(text or "") if " " <= c <= "~")
    return " ".join(out.split())[:LABEL_MAX] or "Reminder"


class Reminders:
    def __init__(self, read=None, write=None, remove=None):
        self._read, self._write, self._remove = read, write, remove
        self.pending = None         # {"occ", "mode", "label", "deadline", "late_s"}
        self.seen = []
        self.results = []           # [[occ, state], ...] newest last
        self.recover()

    # ---- from the server ------------------------------------------------------------------------------
    def submit(self, occ, mode, label, ttl_ms, late_s, now_ms):
        """Accept a reminder from a sync reply. Repeats, unknown modes and spent ttls are ignored (never run twice)."""
        if (isinstance(occ, bool) or not isinstance(occ, int) or occ <= 0 or occ in self.seen
                or isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or ttl_ms <= 0):
            return False
        self.seen.append(occ)
        del self.seen[:-MAX_SEEN]
        if mode not in MODES:
            self._record(occ, "failed")            # a newer server mode this firmware cannot present: say so, never guess
            return False
        self.pending = {"occ": occ, "mode": mode, "label": ascii_label(label), "late_s": late_s if isinstance(late_s, int) else 0,
                        "deadline": ticks_add(now_ms, ttl_ms)}
        return True

    def take(self, now_ms):
        """The reminder to present now (the main loop is idle), or None. One that waited past its grace is dropped and
        reported 'expired': it must not speak at an unexpected moment."""
        item = self.pending
        if item is None:
            return None
        self.pending = None
        if ticks_diff(now_ms, item["deadline"]) >= 0:
            self._record(item["occ"], "expired")
            return None
        return item

    # ---- while presenting ---------------------------------------------------------------------------------
    def begin(self, occ):
        if self._write:
            try:
                self._write(MARKER, str(occ))
            except OSError:
                pass

    def finish(self, occ, state):
        self._record(occ, state)
        if self._remove:
            try:
                self._remove(MARKER)
            except OSError:
                pass

    def recover(self):
        """At boot: a leftover marker means the last reminder was being presented when the Stick reset."""
        if not (self._read and self._remove):
            return
        text = self._read(MARKER)
        if text is None:                # no marker: the normal case
            return
        try:
            occ = int(text.strip())
        except ValueError:
            occ = 0
        if occ > 0:
            self.seen.append(occ)
            self._record(occ, "interrupted")
        try:
            self._remove(MARKER)
        except OSError:
            pass

    def _record(self, occ, state):
        self.results = [r for r in self.results if r[0] != occ]
        self.results.append([occ, state])
        del self.results[:-MAX_RESULTS]

    def report_fields(self):
        return {"rems": [list(r) for r in self.results]}
