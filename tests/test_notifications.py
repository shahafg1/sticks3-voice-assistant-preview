"""Tests for notifications.py. Run: python3 tests/test_notifications.py"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notifications as n  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make():
    clock = Clock()
    return n.NotificationQueue(clock), clock


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def queues_and_returns_a_notification():
    q, _ = make()
    assert q.add("turn_finished", "proj", "s1", "p1") == (True, "queued")
    item = q.next_after(0)
    assert item["kind"] == "turn_finished" and item["label"] == "proj" and item["age"] == 0


@case
def empty_queue_returns_none():
    q, _ = make()
    assert q.next_after(0) is None


@case
def rejects_unknown_kind():
    q, _ = make()
    assert q.add("nonsense", "proj", "s1") == (False, "bad kind")
    assert q.pending() == 0


@case
def duplicate_finished_within_window_is_dropped():
    q, clock = make()
    assert q.add("turn_finished", "proj", "s1", "")[0]
    clock.advance(3)
    assert q.add("turn_finished", "proj", "s1", "") == (False, "duplicate")
    assert q.pending() == 1


@case
def same_prompt_finishing_again_later_is_still_a_duplicate():
    q, clock = make()
    assert q.add("turn_finished", "proj", "s1", "p1")[0]
    clock.advance(60)
    assert q.add("turn_finished", "proj", "s1", "p1") == (False, "duplicate")


@case
def new_prompt_after_window_is_accepted():
    q, clock = make()
    assert q.add("turn_finished", "proj", "s1", "p1")[0]
    clock.advance(20)
    assert q.add("turn_finished", "proj", "s1", "p2")[0]
    assert q.pending() == 1   # coalesced: only the latest matters


@case
def different_sessions_are_independent():
    q, _ = make()
    assert q.add("turn_finished", "a", "s1", "p1")[0]
    assert q.add("turn_finished", "b", "s2", "p2")[0]
    assert q.pending() == 2


@case
def attention_duplicate_window():
    q, clock = make()
    assert q.add("attention", "proj", "s1")[0]
    clock.advance(10)
    assert q.add("attention", "proj", "s1") == (False, "duplicate")
    clock.advance(10)
    assert q.add("attention", "proj", "s1")[0]


@case
def finished_turn_clears_pending_attention_of_same_session():
    q, clock = make()
    assert q.add("attention", "proj", "s1")[0]
    clock.advance(30)
    assert q.add("turn_finished", "proj", "s1", "p1")[0]
    item = q.next_after(0)
    assert item["kind"] == "turn_finished" and q.pending() == 1


@case
def attention_does_not_clear_other_sessions():
    q, _ = make()
    assert q.add("attention", "a", "s1")[0]
    assert q.add("turn_finished", "b", "s2", "p")[0]
    assert q.pending() == 2


@case
def stale_notifications_expire():
    q, clock = make()
    assert q.add("turn_finished", "proj", "s1", "p1")[0]
    clock.advance(n.TTL_S - 1)
    assert q.next_after(0) is not None
    clock.advance(2)
    assert q.next_after(0) is None and q.pending() == 0


@case
def acknowledging_removes_and_advances():
    q, clock = make()
    q.add("turn_finished", "a", "s1", "p1")
    clock.advance(1)
    q.add("turn_finished", "b", "s2", "p2")
    first = q.next_after(0)
    assert first["label"] == "a"
    assert q.next_after(0)["id"] == first["id"]          # not removed until acknowledged
    second = q.next_after(first["id"])
    assert second["label"] == "b" and second["id"] > first["id"]
    assert q.next_after(second["id"]) is None and q.pending() == 0


@case
def ids_increase_even_within_one_millisecond():
    q, _ = make()   # the clock does not move
    q.add("turn_finished", "a", "s1", "p1")
    q.add("turn_finished", "b", "s2", "p2")
    ids = [q.next_after(0)["id"]]
    ids.append(q.next_after(ids[0])["id"])
    assert ids[1] > ids[0]


@case
def queue_is_capped_dropping_the_oldest():
    q, _ = make()
    for i in range(n.MAX_PENDING + 3):
        q.add("turn_finished", f"p{i}", f"s{i}", f"x{i}")
    assert q.pending() == n.MAX_PENDING
    assert q.next_after(0)["label"] == "p3"


@case
def labels_are_cleaned_for_the_stick_font():
    assert n.clean_label("sticks3-project") == "sticks3-project"
    assert n.clean_label("פרויקט") == "Claude"
    assert n.clean_label("my app שלום") == "my app"
    assert n.clean_label("x" * 40) == "x" * n.LABEL_MAX
    assert n.clean_label("<b>hi</b>") == "bhib"
    assert n.clean_label(None) == "Claude"


@case
def recent_table_does_not_grow_forever():
    q, clock = make()
    for i in range(50):
        q.add("turn_finished", "p", f"s{i}", f"x{i}")
    clock.advance(n.PROMPT_DEDUPE_S + 1)
    q.pending()
    assert q._recent == {}


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, error)
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
