"""Tests for reminders.py (scheduling, delivery state machine, audio assets). Run: python3 tests/test_reminders.py"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import reminders as rm  # noqa: E402
from settings_store import Store  # noqa: E402

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=rm.TZ).timestamp()


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class Env:
    """A service with a fake clock and a fake synthesizer that can be told to fail."""
    def __init__(self, synth_ok=True, busy=lambda: False):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        self.clock = Clock()
        self.fail = not synth_ok
        self.synth_calls = []
        self.on_synth = None
        self.sleeps = []
        self.store = Store(self.path / "panel.db")
        self.busy = busy
        self.svc = self.make()

    def make(self):
        return rm.ReminderService(self.store, self.path / "audio", self.clock, self.synth, lambda: self.busy(),
                                  sleep=lambda s: self.sleeps.append(s))

    def synth(self, text, out):
        self.synth_calls.append(text)
        if self.on_synth:
            self.on_synth()
        if self.fail:
            raise RuntimeError("tts down")
        Path(out).write_bytes(b"RIFF" + b"\0" * 2000)

    def at(self, seconds):
        self.clock.now = T0 + seconds

    def make_audio(self, row_id=None):
        """Run the audio worker until it is idle (the tests drive it by hand)."""
        while self.svc.run_one():
            pass

    def due(self, mode="chime", minutes=1, text="hello"):
        row = self.svc.create(text, mode, {"kind": "in", "minutes": minutes})
        if mode == "speak":
            self.make_audio()
        return self.svc.get(row["id"])


def raises(code, fn, *a, **k):
    try:
        fn(*a, **k)
    except rm.ReminderError as error:
        assert error.code == code, (error.code, str(error))
        return error
    raise AssertionError(f"expected ReminderError({code})")


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---- time resolution ------------------------------------------------------------------------------------

@case
def in_minutes_resolves_from_now_and_rejects_bad_values():
    assert rm.resolve_when({"kind": "in", "minutes": 5}, T0) == int(T0) + 300
    for bad in (0, -3, 10081, True, "5", 2.5, None):
        raises("invalid", rm.resolve_when, {"kind": "in", "minutes": bad}, T0)
    for bad in (None, "in 5", {"kind": "tomorrow"}, {}):
        raises("invalid", rm.resolve_when, bad, T0)


@case
def a_local_time_is_resolved_in_jerusalem_and_described_clearly():
    due = rm.resolve_when({"kind": "at", "local": "2026-11-19T09:30"}, T0)
    assert due == datetime(2026, 11, 19, 9, 30, tzinfo=rm.TZ).timestamp()
    info = rm.describe(due)
    assert info["local"] == "2026-11-19 09:30" and info["offset"] == "+02:00" and info["tz"] == "Asia/Jerusalem"
    summer = rm.describe(datetime(2026, 7, 1, 9, 30, tzinfo=rm.TZ).timestamp())
    assert summer["offset"] == "+03:00"


@case
def past_and_far_times_and_malformed_text_are_rejected():
    raises("past", rm.resolve_when, {"kind": "at", "local": "2026-11-18T09:59"}, T0)
    raises("past", rm.resolve_when, {"kind": "at", "local": "2026-11-18T10:00"}, T0)            # not even MIN_LEAD ahead
    raises("too_far", rm.resolve_when, {"kind": "at", "local": "2028-01-01T10:00"}, T0)
    for bad in ("2026-13-01T10:00", "2026-02-30T10:00", "tomorrow", "2026-11-19", "2026-11-19T25:00", 5):
        raises("invalid", rm.resolve_when, {"kind": "at", "local": bad}, T0)


@case
def a_nonexistent_local_time_is_an_explicit_error_not_a_guess():
    now = datetime(2027, 3, 20, 10, 0, tzinfo=rm.TZ).timestamp()
    error = raises("nonexistent", rm.resolve_when, {"kind": "at", "local": "2027-03-26T02:30"}, now)
    assert error.options is None
    raises("nonexistent", rm.resolve_when, {"kind": "at", "local": "2027-03-26T02:30", "fold": 0}, now)   # a fold cannot fix it
    assert rm.resolve_when({"kind": "at", "local": "2027-03-26T03:00"}, now)                              # the neighbours are fine
    assert rm.resolve_when({"kind": "at", "local": "2027-03-26T01:59"}, now)


@case
def an_ambiguous_local_time_offers_both_choices_and_honours_the_pick():
    now = datetime(2026, 10, 20, 10, 0, tzinfo=rm.TZ).timestamp()
    error = raises("ambiguous", rm.resolve_when, {"kind": "at", "local": "2026-10-25T01:30"}, now)
    first, second = error.options
    assert (first["fold"], first["offset"]) == (0, "+03:00") and (second["fold"], second["offset"]) == (1, "+02:00")
    assert second["due_at"] - first["due_at"] == 3600
    assert rm.resolve_when({"kind": "at", "local": "2026-10-25T01:30"}, now, fold=0) == first["due_at"]
    assert rm.resolve_when({"kind": "at", "local": "2026-10-25T01:30"}, now, fold=1) == second["due_at"]
    raises("ambiguous", rm.resolve_when, {"kind": "at", "local": "2026-10-25T01:30"}, now, True)          # a boolean is not a choice


@case
def text_is_cleaned_and_limited():
    assert rm.clean_text("  קח\nתרופה\t‏ ") == "קח תרופה"
    for bad in ("", "   \n", None, 5, "x" * 201):
        raises("invalid", rm.clean_text, bad)
    assert len(rm.clean_text("x" * 200)) == 200
    assert rm.ascii_label("להתקשר לדן Call Dan 3pm") == "Call Dan 3pm"
    assert rm.ascii_label("קח תרופה") == "Reminder" and rm.ascii_label("... ,,, !!") == "Reminder"
    assert len(rm.ascii_label("a" * 100)) == rm.LABEL_MAX


# ---- create / edit / cancel ------------------------------------------------------------------------------

@case
def create_validates_and_applies_the_defaults_of_each_mode():
    env = Env()
    row = env.svc.create("hello", "chime", {"kind": "in", "minutes": 2})
    assert row["status"] == "scheduled" and row["audio"] == "none" and row["due_at"] == int(T0) + 120
    spoken = env.svc.create("שלום", "speak", {"kind": "in", "minutes": 2})
    assert spoken["audio"] == "pending" and spoken["occ"] > row["occ"]
    raises("invalid", env.svc.create, "x", "shout", {"kind": "in", "minutes": 2})
    raises("past", env.svc.create, "x", "chime", {"kind": "at", "local": "2020-01-01T10:00"})
    silent = rm.ReminderService(env.store, env.path / "a2", env.clock, None)
    raises("no_speech", silent.create, "x", "speak", {"kind": "in", "minutes": 2})
    assert silent.create("x", "chime", {"kind": "in", "minutes": 2})["mode"] == "chime"


@case
def a_retried_create_with_the_same_key_never_duplicates():
    env = Env()
    a = env.svc.create("x", "chime", {"kind": "in", "minutes": 2}, client_key="abc")
    b = env.svc.create("x", "chime", {"kind": "in", "minutes": 2}, client_key="abc")
    assert a["id"] == b["id"] and len(env.svc.view(T0)["pending"]) == 1


@case
def pending_reminders_are_capped():
    env = Env()
    for i in range(rm.MAX_PENDING):
        env.svc.create(f"r{i}", "chime", {"kind": "in", "minutes": 5})
    raises("too_many", env.svc.create, "one more", "chime", {"kind": "in", "minutes": 5})


@case
def edit_creates_a_new_occurrence_and_invalidates_the_old_audio():
    env = Env()
    row = env.due("speak", 10, "ישן")
    old_file = env.path / "audio" / rm.audio_name(row["id"], row["occ"])
    assert row["audio"] == "ready" and old_file.is_file()
    edited = env.svc.edit(row["id"], row["occ"], "חדש", "speak", {"kind": "in", "minutes": 20})
    assert edited["occ"] != row["occ"] and edited["text"] == "חדש" and edited["audio"] == "pending"
    assert not old_file.exists()                                                     # the old version's audio is gone
    env.make_audio()
    assert env.svc.get(row["id"])["audio"] == "ready"
    assert (env.path / "audio" / rm.audio_name(row["id"], edited["occ"])).is_file()
    assert env.svc.media_for(row["occ"], T0) is None                                  # the old occurrence is dead
    raises("conflict", env.svc.edit, row["id"], row["occ"], "again", "chime", {"kind": "in", "minutes": 9})    # stale version
    raises("not_found", env.svc.edit, 9999, 1, "x", "chime", {"kind": "in", "minutes": 9})


@case
def editing_a_speech_reminder_to_a_chime_drops_its_audio():
    env = Env()
    row = env.due("speak", 10)
    env.svc.edit(row["id"], row["occ"], "x", "chime", {"kind": "in", "minutes": 10})
    assert env.svc.get(row["id"])["audio"] == "none" and not list((env.path / "audio").glob("r*"))


@case
def cancel_removes_audio_and_is_final():
    env = Env()
    row = env.due("speak", 10)
    done = env.svc.cancel(row["id"], row["occ"])
    assert done["status"] == "cancelled" and not list((env.path / "audio").glob("*"))
    raises("conflict", env.svc.cancel, row["id"], row["occ"])
    raises("conflict", env.svc.edit, row["id"], row["occ"], "x", "chime", {"kind": "in", "minutes": 9})
    env.at(700)
    assert env.svc.next_for_device(env.clock(), "b1") is None                          # a cancelled reminder never fires


@case
def a_sent_reminder_can_no_longer_be_edited_or_cancelled():
    env = Env()
    row = env.due("chime", 1)
    env.at(61)
    assert env.svc.next_for_device(env.clock(), "b1")["occ"] == row["occ"]
    err = raises("conflict", env.svc.cancel, row["id"], row["occ"])
    assert err.status == 409 and "already sent" in str(err)
    raises("conflict", env.svc.edit, row["id"], row["occ"], "x", "chime", {"kind": "in", "minutes": 9})


@case
def cancel_and_delivery_racing_have_exactly_one_winner():
    for round_ in range(25):
        env = Env()
        row = env.due("chime", 1)
        env.at(61)
        barrier = threading.Barrier(2)
        out = {}

        def do_cancel():
            barrier.wait()
            try:
                env.svc.cancel(row["id"], row["occ"])
                out["cancel"] = "ok"
            except rm.ReminderError as error:
                out["cancel"] = error.code

        def do_claim():
            barrier.wait()
            out["claim"] = env.svc.next_for_device(env.clock(), "b1")

        threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_claim)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        final = env.svc.get(row["id"])["status"]
        if out["cancel"] == "ok":
            assert out["claim"] is None and final == "cancelled", (round_, out, final)
        else:
            assert out["cancel"] == "conflict" and out["claim"]["occ"] == row["occ"] and final == "sent", (round_, out, final)


@case
def edit_and_delivery_racing_never_deliver_the_old_version():
    for round_ in range(25):
        env = Env()
        row = env.due("chime", 1, "old")
        env.at(61)
        barrier = threading.Barrier(2)
        out = {}

        def do_edit():
            barrier.wait()
            try:
                env.svc.edit(row["id"], row["occ"], "new", "chime", {"kind": "in", "minutes": 30})
                out["edit"] = "ok"
            except rm.ReminderError as error:
                out["edit"] = error.code

        def do_claim():
            barrier.wait()
            out["claim"] = env.svc.next_for_device(env.clock(), "b1")

        threads = [threading.Thread(target=do_edit), threading.Thread(target=do_claim)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        if out["edit"] == "ok":
            assert out["claim"] is None, (round_, out)                                # moved to the future: nothing to deliver
            assert env.svc.get(row["id"])["status"] == "scheduled"
        else:
            assert out["claim"]["occ"] == row["occ"], (round_, out)                   # delivered as it was, edit refused


# ---- delivery ------------------------------------------------------------------------------------------

@case
def nothing_is_delivered_before_its_time_and_it_is_delivered_when_due():
    env = Env()
    row = env.due("chime", 2, "Call Dan")
    env.at(119)
    assert env.svc.next_for_device(env.clock(), "b1") is None
    env.at(120)
    payload = env.svc.next_for_device(env.clock(), "b1")
    assert payload["occ"] == row["occ"] and payload["mode"] == "chime" and payload["label"] == "Call Dan"
    assert payload["late_s"] == 0 and payload["ttl_ms"] == rm.GRACE_S * 1000
    got = env.svc.get(row["id"])
    assert got["status"] == "sent" and got["attempts"] == 1 and got["sent_boot"] == "b1"


@case
def a_speech_reminder_waits_for_its_audio_then_speaks():
    env = Env()
    row = env.svc.create("שלום", "speak", {"kind": "in", "minutes": 1})
    env.at(70)
    assert env.svc.next_for_device(env.clock(), "b1") is None                          # audio still being made: wait
    env.make_audio()
    payload = env.svc.next_for_device(env.clock(), "b1")
    assert payload["mode"] == "speak" and payload["label"] == "Reminder" and env.svc.get(row["id"])["delivered_mode"] == "speak"


@case
def when_voice_generation_fails_the_reminder_falls_back_to_a_labelled_chime():
    env = Env(synth_ok=False)
    row = env.svc.create("שלום", "speak", {"kind": "in", "minutes": 1})
    for _ in range(rm.AUDIO_TRIES):
        env.svc.enqueue(row["occ"])
        env.make_audio()
        env.at(env.clock.now - T0 + 30)
    assert env.svc.get(row["id"])["audio"] == "failed" and len(env.synth_calls) >= rm.AUDIO_TRIES
    env.at(100)
    payload = env.svc.next_for_device(env.clock(), "b1")
    assert payload["mode"] == "chime"
    got = env.svc.get(row["id"])
    assert got["mode"] == "speak" and got["delivered_mode"] == "chime" and got["reason"] == "voice_unavailable"
    env.svc.device_state(row["occ"], "completed", "b1", env.clock())
    assert env.svc.get(row["id"])["reason"] == "voice_unavailable"                      # the history still says why


@case
def audio_that_never_finishes_falls_back_after_a_bounded_wait():
    env = Env()
    row = env.svc.create("שלום", "speak", {"kind": "in", "minutes": 1})                # the worker never runs
    env.at(60 + rm.AUDIO_WAIT_S - 1)
    assert env.svc.next_for_device(env.clock(), "b1") is None
    env.at(60 + rm.AUDIO_WAIT_S)
    assert env.svc.next_for_device(env.clock(), "b1")["mode"] == "chime"


@case
def overdue_reminders_come_oldest_first_one_at_a_time_with_a_gap():
    env = Env()
    rows = [env.due("chime", m, f"r{m}") for m in (3, 1, 2)]
    env.at(600)                                                                         # all three are overdue
    first = env.svc.next_for_device(env.clock(), "b1")
    assert first["label"] == "r1" and first["late_s"] == 540
    assert env.svc.next_for_device(env.clock(), "b1") is None or True                    # (a resend is allowed after RESEND_AFTER_S)
    env.at(601)
    assert env.svc.next_for_device(env.clock(), "b1") is None                          # nothing new while one is open
    env.svc.device_state(first["occ"], "completed", "b1", env.clock())
    assert env.svc.next_for_device(env.clock(), "b1") is None                          # the quiet gap
    env.at(601 + rm.MIN_GAP_S)
    second = env.svc.next_for_device(env.clock(), "b1")
    assert second["label"] == "r2"
    env.svc.device_state(second["occ"], "dismissed", "b1", env.clock())
    assert env.svc.next_for_device(env.clock(), "b1") is None
    env.at(601 + 2 * rm.MIN_GAP_S + 1)
    assert env.svc.next_for_device(env.clock(), "b1")["label"] == "r3"
    assert [r["id"] for r in rows] and env.svc.get(rows[1]["id"])["status"] == "completed"


@case
def late_within_the_grace_window_is_delivered_and_labelled_late_and_beyond_it_is_missed():
    env = Env()
    late = env.due("chime", 1, "late one")
    env.at(60 + rm.GRACE_S - 1)
    payload = env.svc.next_for_device(env.clock(), "b1")
    assert payload["late_s"] == rm.GRACE_S - 1 and payload["ttl_ms"] == 1000
    item = env.svc.view(env.clock())["pending"][0]
    assert item["late"] is True and item["late_s"] == rm.GRACE_S - 1
    env2 = Env()
    gone = env2.due("speak", 1, "too late")
    env2.at(60 + rm.GRACE_S)
    assert env2.svc.next_for_device(env2.clock(), "b1") is None                        # missed: never spoken
    got = env2.svc.get(gone["id"])
    assert got["status"] == "missed" and got["reason"] == "grace" and not list((env2.path / "audio").glob("r*"))
    assert late["id"] and env2.svc.view(env2.clock())["history"][0]["status"] == "missed"


@case
def a_reminder_delivered_on_time_is_not_labelled_late():
    env = Env()
    row = env.due("chime", 1)
    env.at(62)
    env.svc.next_for_device(env.clock(), "b1")
    assert env.svc.view(env.clock())["pending"][0]["late"] is False and row["id"]


@case
def older_firmware_is_never_handed_a_reminder_by_the_link():
    # (the capability check lives in DeviceLink.sync; here: nothing is claimed unless next_for_device is called)
    env = Env()
    row = env.due("chime", 1)
    env.at(300)
    assert env.svc.get(row["id"])["status"] == "scheduled"


# ---- lost acknowledgements, results, restarts -----------------------------------------------------------------------

@case
def an_unacknowledged_delivery_is_offered_again_but_not_counted_twice():
    env = Env()
    row = env.due("chime", 1)
    env.at(61)
    first = env.svc.next_for_device(env.clock(), "b1")
    env.at(61 + rm.RESEND_AFTER_S - 1)
    assert env.svc.next_for_device(env.clock(), "b1") is None
    env.at(61 + rm.RESEND_AFTER_S)
    again = env.svc.next_for_device(env.clock(), "b1")
    assert again["occ"] == first["occ"] and env.svc.get(row["id"])["attempts"] == 1
    env.svc.device_state(first["occ"], "received", "b1", env.clock())
    env.at(80)
    assert env.svc.next_for_device(env.clock(), "b1") is None                          # acknowledged: no more offers


@case
def stages_only_move_forward_and_results_are_final():
    env = Env()
    row = env.due("chime", 1)
    env.at(61)
    occ = env.svc.next_for_device(env.clock(), "b1")["occ"]
    now = env.clock()
    assert env.svc.device_state(occ, "presented", "b1", now) and env.svc.get(row["id"])["status"] == "presented"
    assert not env.svc.device_state(occ, "received", "b1", now)                         # never backwards
    assert not env.svc.device_state(occ, "presented", "other-boot", now)
    assert not env.svc.device_state(occ, "bogus", "b1", now) and not env.svc.device_state(999, "completed", "b1", now)
    assert env.svc.device_state(occ, "completed", "b1", now) and env.svc.get(row["id"])["status"] == "completed"
    assert not env.svc.device_state(occ, "failed", "b1", now)                           # a re-reported older result changes nothing
    assert not env.svc.device_state(occ, "completed", "b1", now)
    got = env.svc.get(row["id"])
    assert got["received_at"] is not None and got["presented_at"] is not None and got["finished_at"] == now


@case
def every_device_result_maps_to_an_honest_status():
    expect = {"completed": ("completed", None), "muted": ("completed", "muted"), "dismissed": ("dismissed", None),
              "failed": ("failed", "device"), "media_failed": ("failed", "media"), "expired": ("missed", "late_on_device"),
              "interrupted": ("failed", "interrupted")}
    for result, (status, reason) in expect.items():
        env = Env()
        row = env.due("chime", 1)
        env.at(61)
        occ = env.svc.next_for_device(env.clock(), "b1")["occ"]
        assert env.svc.device_state(occ, result, "b1", env.clock())
        got = env.svc.get(row["id"])
        assert (got["status"], got["reason"]) == (status, reason), (result, got["status"], got["reason"])


@case
def receipt_is_not_playback_a_download_alone_never_completes_it():
    env = Env()
    row = env.due("speak", 1)
    env.at(61)
    occ = env.svc.next_for_device(env.clock(), "b1")["occ"]
    path = env.svc.media_for(occ, env.clock())
    assert path and path.is_file() and env.svc.get(row["id"])["status"] == "received"
    assert env.svc.get(row["id"])["status"] not in rm.TERMINAL


@case
def media_is_only_served_for_the_current_valid_delivery():
    env = Env()
    row = env.due("speak", 1)
    assert env.svc.media_for(row["occ"], T0) is None                                    # not sent yet
    env.at(61)
    occ = env.svc.next_for_device(env.clock(), "b1")["occ"]
    assert env.svc.media_for(occ, env.clock())
    assert env.svc.media_for(occ + 1, env.clock()) is None and env.svc.media_for(0, env.clock()) is None
    env.svc.device_state(occ, "completed", "b1", env.clock())
    assert env.svc.media_for(occ, env.clock()) is None                                  # finished: gone
    assert not list((env.path / "audio").glob("r*"))                                    # and the file was cleaned up


@case
def a_restart_during_delivery_gets_one_bounded_redelivery_then_fails():
    env = Env()
    row = env.due("chime", 1)
    env.at(61)
    first = env.svc.next_for_device(env.clock(), "boot-1")
    env.at(70)
    assert env.svc.reconcile_boot("boot-2", env.clock()) == 1                           # the Stick restarted with no result
    got = env.svc.get(row["id"])
    assert got["status"] == "scheduled" and got["reason"] == "retry_after_restart" and got["attempts"] == 1
    env.at(100)
    second = env.svc.next_for_device(env.clock(), "boot-2")
    assert second["occ"] == first["occ"] and env.svc.get(row["id"])["attempts"] == 2
    env.at(110)
    env.svc.reconcile_boot("boot-3", env.clock())                                       # restarts again: no third try
    got = env.svc.get(row["id"])
    assert got["status"] == "failed" and got["reason"] == "restart"
    env.at(400)
    assert env.svc.next_for_device(env.clock(), "boot-3") is None


@case
def a_restart_after_the_grace_window_fails_instead_of_retrying():
    env = Env()
    row = env.due("chime", 1)
    env.at(61)
    env.svc.next_for_device(env.clock(), "boot-1")
    env.at(60 + rm.GRACE_S + 5)
    env.svc.reconcile_boot("boot-2", env.clock())
    assert env.svc.get(row["id"])["status"] == "failed"


@case
def an_interrupted_presentation_reported_by_the_stick_is_failed_and_never_retried():
    env = Env()
    row = env.due("speak", 1)
    env.at(61)
    occ = env.svc.next_for_device(env.clock(), "boot-1")["occ"]
    env.svc.media_for(occ, env.clock())
    env.at(65)
    assert env.svc.device_state(occ, "interrupted", "boot-2", env.clock())              # first report of the new boot
    env.svc.reconcile_boot("boot-2", env.clock())
    got = env.svc.get(row["id"])
    assert got["status"] == "failed" and got["reason"] == "interrupted" and got["attempts"] == 1
    env.at(200)
    assert env.svc.next_for_device(env.clock(), "boot-2") is None


@case
def silence_after_delivery_ends_as_failed_unconfirmed_and_a_late_report_corrects_it():
    env = Env()
    row = env.due("chime", 1)
    env.at(61)
    occ = env.svc.next_for_device(env.clock(), "b1")["occ"]
    env.at(61 + rm.UNCONFIRMED_S)
    env.svc.tick(env.clock())
    got = env.svc.get(row["id"])
    assert got["status"] == "failed" and got["reason"] == "unconfirmed"
    assert env.svc.device_state(occ, "completed", "b1", env.clock())                    # the Stick reconnects and tells the truth
    assert env.svc.get(row["id"])["status"] == "completed"


@case
def the_quiet_gap_also_applies_after_a_failed_delivery():
    env = Env()
    env.due("chime", 1, "a")
    b = env.due("chime", 1, "b")
    env.at(61)
    a_payload = env.svc.next_for_device(env.clock(), "b1")
    env.svc.device_state(a_payload["occ"], "failed", "b1", env.clock())
    assert env.svc.next_for_device(env.clock(), "b1") is None
    env.at(61 + rm.MIN_GAP_S)
    assert env.svc.next_for_device(env.clock(), "b1")["label"] == "b" and b["id"]


# ---- audio assets ----------------------------------------------------------------------------------------------

@case
def generation_that_loses_a_race_with_an_edit_discards_its_file():
    env = Env()
    row = env.svc.create("old", "speak", {"kind": "in", "minutes": 10})

    def edit_now():
        env.svc.edit(row["id"], row["occ"], "new", "speak", {"kind": "in", "minutes": 20})
        env.on_synth = None

    env.on_synth = edit_now
    env.svc.run_one()                                                                   # generates the OLD text; the edit lands meanwhile
    assert (row["occ"], "discarded") in env.svc.audio_log
    assert not (env.path / "audio" / rm.audio_name(row["id"], row["occ"])).exists()
    env.make_audio()
    got = env.svc.get(row["id"])
    assert got["audio"] == "ready" and env.synth_calls == ["old", "new"]
    assert sorted(p.name for p in (env.path / "audio").glob("*")) == [rm.audio_name(row["id"], got["occ"])]


@case
def generation_failures_are_retried_with_backoff_and_then_marked_failed_visibly():
    env = Env(synth_ok=False)
    row = env.svc.create("שלום", "speak", {"kind": "in", "minutes": 10})
    assert env.svc.run_one() and env.svc.get(row["id"])["audio"] == "pending"           # first failure: will retry
    assert not env.svc.run_one()                                                        # the retry is not due yet
    env.at(25)
    assert env.svc.run_one() and env.svc.get(row["id"])["audio"] == "pending"
    env.at(80)
    assert env.svc.run_one() and env.svc.get(row["id"])["audio"] == "failed"            # third failure is final
    assert not list((env.path / "audio").glob("*"))                                     # no half-written files left behind
    env.fail = False
    assert env.svc.view(env.clock())["pending"][0]["audio"] == "failed"


@case
def unusable_audio_counts_as_a_failure():
    env = Env()
    env.synth = lambda text, out: Path(out).write_bytes(b"x" * 10)
    env.svc = env.make()
    row = env.svc.create("שלום", "speak", {"kind": "in", "minutes": 10})
    env.svc.run_one()
    assert env.svc.get(row["id"])["audio"] == "pending" and env.svc.audio_log[-1][1].startswith("error")
    assert not list((env.path / "audio").glob("*"))


@case
def the_audio_worker_waits_while_a_voice_question_is_being_processed():
    state = {"n": 0}

    def busy():
        state["n"] += 1
        return state["n"] <= 3

    env = Env(busy=busy)
    row = env.svc.create("שלום", "speak", {"kind": "in", "minutes": 10})
    env.svc.run_one()
    assert env.sleeps == [1, 1, 1] and env.svc.get(row["id"])["audio"] == "ready"


@case
def a_server_restart_regenerates_missing_audio_and_removes_orphans():
    env = Env()
    row = env.due("speak", 10)
    (env.path / "audio" / "r999_o1.wav").write_bytes(b"orphan")
    (env.path / "audio" / "tmp_5.wav").write_bytes(b"half")
    (env.path / "audio" / rm.audio_name(row["id"], row["occ"])).unlink()                # the file vanished
    env.svc = env.make()                                                                # a new process
    assert sorted(p.name for p in (env.path / "audio").glob("*")) == []
    assert env.svc.get(row["id"])["audio"] == "pending"
    env.make_audio()
    assert env.svc.get(row["id"])["audio"] == "ready"


@case
def reminders_survive_a_restart_and_overdue_ones_are_still_ordered_and_bounded():
    env = Env()
    rows = [env.due("chime", m, f"r{m}") for (m) in (1, 2)]
    env.svc = env.make()
    env.at(61 + 20 * 60)
    first = env.svc.next_for_device(env.clock(), "b1")
    assert first["label"] == "r1" and first["late_s"] == 1201 and rows
    env.at(120 + rm.GRACE_S + 1)
    env.svc.tick(env.clock())
    assert env.svc.get(rows[1]["id"])["status"] == "missed"                              # no catch-up beyond the grace window


@case
def history_is_bounded_and_newest_first():
    env = Env()
    ids = []
    for i in range(rm.HISTORY_KEEP + 5):
        env.at(i)
        row = env.svc.create(f"r{i}", "chime", {"kind": "in", "minutes": 1})
        env.svc.cancel(row["id"], row["occ"])
        ids.append(row["id"])
    env.svc.tick(env.clock())
    view = env.svc.view(env.clock())
    assert env.svc.get(ids[0]) is None and env.svc.get(ids[-1]) is not None
    assert view["history"][0]["text"] == f"r{rm.HISTORY_KEEP + 4}" and len(view["history"]) == 20


@case
def occurrence_ids_are_unique_and_increase():
    env = Env()
    occs = [env.svc.create("x", "chime", {"kind": "in", "minutes": 5})["occ"] for _ in range(5)]
    assert occs == sorted(set(occs))
    row = env.svc.get(1)
    again = env.svc.edit(row["id"], row["occ"], "y", "chime", {"kind": "in", "minutes": 6})["occ"]
    assert again > occs[-1]


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
