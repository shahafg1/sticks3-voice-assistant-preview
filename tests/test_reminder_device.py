"""Tests for stick/reminder.py and its hooks in stick/device.py. Run: python3 tests/test_reminder_device.py"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stick"))
import device  # noqa: E402
import reminder as rem  # noqa: E402

T0 = 1_000_000


class Files:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def read(self, path):
        return self.data.get(path)

    def write(self, path, text):
        self.data[path] = text

    def remove(self, path):
        if path not in self.data:
            raise OSError("missing")
        del self.data[path]


def make(files=None):
    files = files or Files()
    return rem.Reminders(files.read, files.write, files.remove), files


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def a_submitted_reminder_is_taken_once_while_idle():
    r, _ = make()
    assert r.submit(7, "speak", "Call Dan", 60_000, 0, T0)
    item = r.take(T0 + 1000)
    assert item["occ"] == 7 and item["mode"] == "speak" and item["label"] == "Call Dan"
    assert r.take(T0 + 1001) is None and r.pending is None


@case
def repeated_offers_of_the_same_reminder_are_ignored_even_after_it_finished():
    r, _ = make()
    assert r.submit(7, "chime", "x", 60_000, 0, T0)
    assert not r.submit(7, "chime", "x", 60_000, 0, T0 + 3000)                      # a lost reply makes the server offer it again
    r.take(T0 + 3000)
    r.finish(7, "completed")
    assert not r.submit(7, "chime", "x", 60_000, 0, T0 + 6000)                      # ...and still no second alert after the result
    assert r.pending is None and r.results == [[7, "completed"]]


@case
def ids_need_not_increase_because_reminders_are_delivered_by_due_time():
    r, _ = make()
    assert r.submit(500, "chime", "a", 60_000, 0, T0) and r.take(T0)
    assert r.submit(100, "chime", "b", 60_000, 0, T0 + 1)                           # an older id with an earlier-created reminder is valid


@case
def invalid_offers_are_refused():
    r, _ = make()
    for args in ((0, "chime", "x", 1000), (-1, "chime", "x", 1000), (True, "chime", "x", 1000), (5, "chime", "x", 0),
                 (5, "chime", "x", -5), (5, "chime", "x", True), ("5", "chime", "x", 1000)):
        assert not r.submit(args[0], args[1], args[2], args[3], 0, T0), args
    assert r.pending is None


@case
def a_mode_this_firmware_cannot_present_is_reported_failed_not_guessed():
    r, _ = make()
    assert not r.submit(9, "hologram", "x", 60_000, 0, T0)
    assert r.pending is None and r.results == [[9, "failed"]]


@case
def a_reminder_that_waited_past_its_grace_is_dropped_and_reported_expired():
    r, _ = make()
    r.submit(3, "speak", "x", 5_000, 0, T0)
    assert r.take(T0 + 5_000) is None and r.results == [[3, "expired"]]


@case
def the_label_is_ascii_only_and_bounded():
    assert rem.ascii_label("קח תרופה") == "Reminder" and rem.ascii_label(None) == "Reminder"
    assert rem.ascii_label("Call  Dan א 3pm") == "Call Dan 3pm"
    assert len(rem.ascii_label("a" * 90)) == rem.LABEL_MAX
    r, _ = make()
    r.submit(1, "chime", "שלום\x00\x7fOK", 1000, 0, T0)
    assert r.pending["label"] == "OK"


@case
def results_are_capped_newest_last_and_one_per_occurrence():
    r, _ = make()
    for occ in range(1, 8):
        r.finish(occ, "completed")
    assert r.results == [[4, "completed"], [5, "completed"], [6, "completed"], [7, "completed"]]
    r.finish(6, "dismissed")
    assert r.results[-1] == [6, "dismissed"] and len(r.results) == 4
    assert r.report_fields() == {"rems": r.results} and r.report_fields()["rems"] is not r.results


@case
def the_seen_list_is_bounded():
    r, _ = make()
    for occ in range(1, 40):
        r.submit(occ, "chime", "x", 1000, 0, T0)
    assert len(r.seen) == rem.MAX_SEEN


@case
def a_reset_while_presenting_is_reported_interrupted_at_the_next_boot():
    r, files = make()
    r.submit(11, "speak", "x", 60_000, 0, T0)
    item = r.take(T0)
    r.begin(item["occ"])
    assert files.data[rem.MARKER] == "11"
    reborn, _ = make(files)                                                          # power cycle: RAM lost, flash kept
    assert reborn.results == [[11, "interrupted"]] and rem.MARKER not in files.data
    assert not reborn.submit(11, "speak", "x", 60_000, 0, T0 + 5000)                 # a repeated offer is not presented again
    assert reborn.pending is None


@case
def a_normal_finish_removes_the_marker_so_a_later_boot_reports_nothing():
    r, files = make()
    r.begin(12)
    r.finish(12, "completed")
    assert rem.MARKER not in files.data
    again, _ = make(files)
    assert again.results == []


@case
def a_corrupt_or_missing_marker_is_harmless():
    for text in ("garbage", "", "-4", "0"):
        files = Files({rem.MARKER: text})
        r, _ = make(files)
        assert r.results == [] and rem.MARKER not in files.data, text
    r = rem.Reminders()                                                              # no file functions at all (tests, CPython)
    r.begin(1)
    r.finish(1, "completed")
    assert r.results == [[1, "completed"]]


@case
def flash_errors_never_stop_a_reminder():
    class Broken(Files):
        def write(self, path, text):
            raise OSError("full")

        def remove(self, path):
            raise OSError("busy")

    files = Broken()
    r = rem.Reminders(files.read, files.write, files.remove)
    r.begin(5)
    r.finish(5, "completed")
    assert r.results == [[5, "completed"]]


# ---- DeviceState / DeviceSync hooks ------------------------------------------------------------------------------

class FakeLcd:
    def __init__(self):
        self.raw = 128

    def getBrightness(self):
        return self.raw

    def setBrightness(self, raw):
        self.raw = raw


def sync_reply(**extra):
    body = {"store": "S", "rev": 1, "settings": {}, "commands": [], "notification": None, "poll_ms": 3000}
    body.update(extra)
    return json.dumps(body).encode()


@case
def the_stick_advertises_the_reminder_capability_and_reports_results():
    files = Files()
    state = device.DeviceState(FakeLcd(), None, None, "fw", read=files.read, write=files.write, remove=files.remove)
    assert "reminder" in device.CAPS and device.APP_VERSION.startswith("0.9.0")
    state.reminders.finish(4, "completed")
    report = state.report(0, now_ms=1000)
    assert report["rems"] == [[4, "completed"]] and "reminder" in report["caps"]


@case
def a_sync_reply_hands_the_reminder_to_the_idle_loop_and_old_servers_send_none():
    files = Files()
    state = device.DeviceState(FakeLcd(), None, None, "fw", read=files.read, write=files.write, remove=files.remove)
    sync = device.DeviceSync(state, key="k" * 40)
    sync._handle(200, sync_reply(), T0)
    assert state.reminders.pending is None                                           # a v0.8.0 server: no reminder key at all
    sync._handle(200, sync_reply(reminder={"occ": 8, "mode": "speak", "label": "Pills", "ttl_ms": 90_000, "late_s": 3}), T0)
    assert state.reminders.pending["occ"] == 8 and state.reminders.pending["late_s"] == 3
    sync._handle(200, sync_reply(reminder={"occ": 8, "mode": "speak", "label": "Pills", "ttl_ms": 90_000, "late_s": 3}), T0 + 3000)
    taken = state.reminders.take(T0 + 3500)
    assert taken["occ"] == 8
    sync._handle(200, sync_reply(reminder={"occ": 8, "mode": "speak", "ttl_ms": 90_000}), T0 + 6000)
    assert state.reminders.pending is None                                           # offered again after taking: still ignored


@case
def malformed_reminder_payloads_never_break_the_sync():
    files = Files()
    state = device.DeviceState(FakeLcd(), None, None, "fw", read=files.read, write=files.write, remove=files.remove)
    sync = device.DeviceSync(state, key="k" * 40)
    for bad in ("text", 5, [], {"occ": "x", "mode": "chime", "ttl_ms": 1000}, {"occ": 1, "mode": 5, "ttl_ms": 1000},
                {"occ": 1, "mode": "chime"}, {"occ": 1, "mode": "chime", "ttl_ms": "soon"}):
        assert sync._handle(200, sync_reply(reminder=bad), T0) is None
    assert state.reminders.pending is None and sync.mode == "sync"


@case
def the_settings_cache_is_unaffected_by_reminders():
    files = Files()
    state = device.DeviceState(FakeLcd(), None, None, "fw", read=files.read, write=files.write, remove=files.remove)
    state.reminders.finish(1, "completed")
    state.apply("S", 1, {"brightness": 60})
    state.maybe_save(0)
    cache = json.loads(files.data[device.CACHE_FILE])
    assert set(cache) == {"store", "rev", "brightness", "idle_timeout_s", "sleep_mode"}       # nothing new, no extra flash writes


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:300])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:300])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
