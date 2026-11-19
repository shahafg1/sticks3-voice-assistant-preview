"""Pending Claude Code notifications for the Stick (pure logic, no I/O).

Hooks on the Mac add events; the Stick asks for the next one while it is idle.
Events are deduplicated, expire after TTL_S, and are kept until the Stick
acknowledges them by asking for "ids after N".
"""
import re
import threading
import time

TTL_S = 300               # a notification older than this is stale and dropped
MAX_PENDING = 5           # oldest are dropped beyond this
DEDUPE_FINISHED_S = 8     # same session, "turn finished" again within this: duplicate
DEDUPE_ATTENTION_S = 15   # same session, "attention" again within this: duplicate
PROMPT_DEDUPE_S = 600     # same session and prompt id finishing twice: duplicate
LABEL_MAX = 16            # the Stick's small font fits about 16 characters

KINDS = ("turn_finished", "attention")


def clean_label(text):
    """ASCII-only short label (the Stick font has no Hebrew)."""
    text = re.sub(r"[^A-Za-z0-9._ \-]", "", str(text or "")).strip()
    return text[:LABEL_MAX] or "Claude"


class NotificationQueue:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._items = []     # {"id", "kind", "label", "session", "created"}
        self._recent = {}    # dedupe key -> time it was last accepted
        self._last_id = 0

    def add(self, kind, label, session="", prompt=""):
        """Queue an event. Returns (accepted, reason)."""
        if kind not in KINDS:
            return False, "bad kind"
        session = str(session or "")[:64]
        prompt = str(prompt or "")[:64]
        with self._lock:
            now = self._clock()
            self._expire(now)

            window = DEDUPE_FINISHED_S if kind == "turn_finished" else DEDUPE_ATTENTION_S
            key = (session, kind)
            if now - self._recent.get(key, -1e12) < window:
                return False, "duplicate"
            prompt_key = (session, kind, prompt)
            if kind == "turn_finished" and prompt:
                if now - self._recent.get(prompt_key, -1e12) < PROMPT_DEDUPE_S:
                    return False, "duplicate"
                self._recent[prompt_key] = now
            self._recent[key] = now

            # Only the latest event per session and kind matters, and a finished
            # turn means an earlier "needs attention" from that session is moot.
            drop = {kind}
            if kind == "turn_finished":
                drop.add("attention")
            self._items = [i for i in self._items
                           if not (i["session"] == session and i["kind"] in drop)]

            item_id = max(self._last_id + 1, int(now * 1000))
            self._last_id = item_id
            self._items.append({"id": item_id, "kind": kind, "label": clean_label(label),
                                "session": session, "created": now})
            del self._items[:-MAX_PENDING]
            return True, "queued"

    def next_after(self, after_id=0):
        """First pending notification newer than after_id, as a dict, or None.

        Asking for ids after N acknowledges (removes) everything up to N."""
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._items = [i for i in self._items if i["id"] > after_id]
            if not self._items:
                return None
            item = self._items[0]
            return {"id": item["id"], "kind": item["kind"], "label": item["label"],
                    "age": int(now - item["created"])}

    def pending(self):
        with self._lock:
            self._expire(self._clock())
            return len(self._items)

    def _expire(self, now):
        self._items = [i for i in self._items if now - i["created"] <= TTL_S]
        self._recent = {k: t for k, t in self._recent.items() if now - t <= PROMPT_DEDUPE_S}
