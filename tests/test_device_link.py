"""Tests for device_link.py (status rules, authentication, sync, restart). Run: python3 tests/test_device_link.py"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import device_link as dl  # noqa: E402

KEY = "k" * 48


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Env:
    """A DeviceLink on temporary directories; restart() builds a new one on the same data."""
    def __init__(self, key=KEY, notifications=None):
        self.dir = tempfile.TemporaryDirectory()
        self.data = Path(self.dir.name) / "data"
        self.secrets = Path(self.dir.name) / "secrets"
        self.secrets.mkdir()
        if key is not None:
            (self.secrets / "device_key").write_text(key + "\n")
            os.chmod(self.secrets / "device_key", 0o600)
        self.clock = Clock()
        self.notifications = notifications or (lambda after, ip: None)
        self.busy = False
        self.link = self.make()

    def make(self):
        return dl.DeviceLink(lambda a, ip: self.notifications(a, ip), busy_hint=lambda: self.busy,
                             data_dir=self.data, secrets_dir=self.secrets, clock=self.clock)

    def restart(self):
        self.link = self.make()
        return self.link

    def sync(self, **fields):
        body = {"proto": 1, "store": "", "rev": 0, "notif": 0}
        body.update(fields)
        key = fields.pop("_key", KEY) if "_key" in fields else KEY
        return self.link.sync(key, json.dumps({k: v for k, v in body.items() if k != "_key"}).encode(), "10.0.0.12")

    def report_matching(self, **extra):
        store, rev, settings, _ = self.link.store.desired()
        return self.sync(store=store, rev=rev, brightness=settings.get("brightness"), **extra)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---- status rules --------------------------------------------------------------------------

@case
def contact_classes_have_exact_boundaries():
    c = dl.classify_contact
    assert c(None) == "never"
    assert c(0) == c(dl.FRESH_S) == "fresh"
    assert c(dl.FRESH_S + 0.01) == c(dl.GAP_S) == "gap"
    assert c(dl.GAP_S + 0.01) == "stale"


@case
def brightness_status_truth_table():
    f = dl.brightness_status
    assert f("S", 3, {}, None) == "unset" and f("S", 3, {}, {"store": "S", "rev": 3}) == "unset"
    assert f("S", 3, {"brightness": 70}, None) == "pending"
    assert f("S", 3, {"brightness": 70}, {"store": "S", "rev": 3, "brightness": 70}) == "confirmed"
    assert f("S", 3, {"brightness": 70}, {"store": "S", "rev": 3, "brightness": 65}) == "differs"
    assert f("S", 3, {"brightness": 70}, {"store": "S", "rev": 2, "brightness": 70}) == "pending"   # old revision
    assert f("S", 3, {"brightness": 70}, {"store": "OTHER", "rev": 3, "brightness": 70}) == "pending"  # other store
    assert f("S", 3, {"brightness": 70}, {"store": "S", "rev": 3}) == "differs"                       # no value reported


@case
def report_is_validated_and_whitelisted():
    ok = dl.clean_report({"proto": 1, "store": "ab", "rev": 3, "notif": 7, "brightness": 70, "battery": 88,
                          "charging": True, "rssi": -60, "uptime": 99, "app": "0.7.0", "fw": "V2.4.8",
                          "boot": "abcd", "evil": "x"})
    assert "evil" not in ok and ok["brightness"] == 70 and ok["charging"] is True
    for bad in ({"proto": 2}, {}, [], None, {"proto": 1, "rev": -1}, {"proto": 1, "brightness": 101},
                {"proto": 1, "brightness": True}, {"proto": 1, "brightness": "70"}, {"proto": 1, "rssi": 5},
                {"proto": 1, "rev": 2 ** 40}):
        try:
            dl.clean_report(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    odd = dl.clean_report({"proto": 1, "app": "ש\x00lom<script>" + "x" * 100})
    assert odd["app"] == "lom<script>" + "x" * 21 and len(odd["app"]) == 32


# ---- authentication --------------------------------------------------------------------------

@case
def key_is_required_and_checked_before_the_body():
    e = Env()
    assert e.link.sync(None, b"{}", "1.2.3.4")[0] == 401
    assert e.link.sync("wrong", b"not json at all", "1.2.3.4")[0] == 401          # auth first: no 400
    assert e.link.sync(KEY, b"not json at all", "1.2.3.4")[0] == 400
    assert e.sync()[0] == 200
    e.dir.cleanup()


@case
def failed_authentication_changes_nothing():
    e = Env()
    e.link.set_brightness(70)
    before = (e.link.store.desired(), e.link.snapshot()["device"]["contact"])
    for key in (None, "", "wrong", KEY[:-1], KEY + "x"):
        status, data = e.link.sync(key, json.dumps({"proto": 1, "store": "x", "rev": 99, "brightness": 70}).encode(), "9.9.9.9")
        assert status == 401 and data == {"error": "unauthorized"}
    assert (e.link.store.desired(), e.link.snapshot()["device"]["contact"]) == before == (before[0], "never")
    e.dir.cleanup()


@case
def server_without_a_key_rejects_everything():
    e = Env(key=None)
    assert not e.link.key_provisioned
    assert e.sync()[0] == 401 and e.link.sync("anything", b"{}", "1.1.1.1")[0] == 401
    e.dir.cleanup()


@case
def too_short_key_file_is_ignored():
    e = Env(key="short")
    assert not e.link.key_provisioned and e.sync()[0] == 401
    e.dir.cleanup()


@case
def repeated_failures_lock_out_that_client_only_for_a_while():
    e = Env()
    for _ in range(dl.AUTH_FAILS_PER_MIN):
        e.link.sync("bad", b"{}", "6.6.6.6")
    status, _ = e.link.sync(KEY, json.dumps({"proto": 1}).encode(), "6.6.6.6")
    assert status == 429                                                       # even the right key, while locked out
    assert e.link.sync(KEY, json.dumps({"proto": 1}).encode(), "7.7.7.7")[0] == 200   # other clients unaffected
    e.clock.advance(61)
    assert e.link.sync(KEY, json.dumps({"proto": 1}).encode(), "6.6.6.6")[0] == 200
    e.dir.cleanup()


@case
def key_never_appears_in_any_output():
    e = Env()
    e.link.set_brightness(70)
    outputs = [e.sync()[1], e.link.snapshot(), e.link.sync("bad", b"{}", "5.5.5.5")[1], e.link.sync(None, b"{}", "5.5.5.5")[1]]
    assert KEY not in json.dumps(outputs)
    e.dir.cleanup()


# ---- brightness lifecycle ---------------------------------------------------------------------

@case
def pending_then_confirmed_only_after_the_device_reports():
    e = Env()
    e.link.set_brightness(70)
    snap = e.link.snapshot()
    assert snap["brightness"]["state"] == "pending" and snap["device"]["contact"] == "never"
    status, data = e.sync(store="", rev=0)                                    # the Stick asks and learns the target
    assert status == 200 and data["settings"] == {"brightness": 70} and data["rev"] == 1
    assert e.link.snapshot()["brightness"]["state"] == "pending"               # asking is not confirming
    e.sync(store=data["store"], rev=1, brightness=70)                          # the device acknowledges
    snap = e.link.snapshot()
    assert snap["brightness"]["state"] == "confirmed" and snap["device"]["contact"] == "fresh"
    e.dir.cleanup()


@case
def device_that_applied_another_value_is_flagged():
    e = Env()
    e.link.set_brightness(70)
    store = e.link.store.desired()[0]
    e.sync(store=store, rev=1, brightness=65)
    assert e.link.snapshot()["brightness"]["state"] == "differs"
    e.dir.cleanup()


@case
def new_change_makes_a_confirmed_value_pending_again():
    e = Env()
    e.link.set_brightness(70)
    e.report_matching()
    assert e.link.snapshot()["brightness"]["state"] == "confirmed"
    e.link.set_brightness(30)
    assert e.link.snapshot()["brightness"]["state"] == "pending"
    e.report_matching()
    assert e.link.snapshot()["brightness"]["state"] == "confirmed"
    e.dir.cleanup()


@case
def offline_changes_coalesce_to_the_latest_value():
    e = Env()
    for value in (10, 90, 40, 75):
        e.link.set_brightness(value)                                           # the Stick hears nothing meanwhile
    status, data = e.sync(store="", rev=0)
    assert data["settings"] == {"brightness": 75} and data["rev"] == 4
    e.dir.cleanup()


@case
def same_revision_from_another_store_never_counts_as_confirmed():
    e = Env()
    e.link.set_brightness(70)
    e.sync(store="someoldstore0000", rev=1, brightness=70)
    snap = e.link.snapshot()
    assert snap["brightness"]["state"] == "pending"
    status, data = e.sync(store="someoldstore0000", rev=1, brightness=70)
    assert data["store"] != "someoldstore0000"                                  # the answer names the real store
    e.dir.cleanup()


@case
def contact_ages_through_fresh_gap_stale():
    e = Env()
    e.sync()
    assert e.link.snapshot()["device"]["contact"] == "fresh"
    e.clock.advance(11)
    assert e.link.snapshot()["device"]["contact"] == "gap"
    e.clock.advance(300)
    snap = e.link.snapshot()
    assert snap["device"]["contact"] == "stale" and snap["device"]["age_s"] > 300
    e.dir.cleanup()


@case
def a_voice_job_is_only_context_during_a_gap():
    e = Env()
    e.busy = True
    assert e.link.snapshot()["device"]["voice_job_active"] is False            # never reported: a job proves nothing
    e.sync()
    assert e.link.snapshot()["device"]["voice_job_active"] is False            # fresh: not needed
    e.clock.advance(30)
    snap = e.link.snapshot()
    assert snap["device"]["contact"] == "gap" and snap["device"]["voice_job_active"] is True
    e.clock.advance(400)
    snap = e.link.snapshot()
    assert snap["device"]["contact"] == "stale" and snap["device"]["voice_job_active"] is False
    e.dir.cleanup()


@case
def poll_hint_is_fast_after_a_change_and_while_unconfirmed():
    e = Env()
    assert e.sync()[1]["poll_ms"] == dl.POLL_NORMAL_MS
    e.link.set_brightness(70)
    assert e.sync(store="", rev=0)[1]["poll_ms"] == dl.POLL_FAST_MS
    store = e.link.store.desired()[0]
    e.clock.advance(dl.FAST_AFTER_CHANGE_S + 1)
    assert e.sync(store=store, rev=0)[1]["poll_ms"] == dl.POLL_FAST_MS         # still unconfirmed
    e.clock.advance(dl.FAST_WHILE_PENDING_S)
    assert e.sync(store=store, rev=0)[1]["poll_ms"] == dl.POLL_NORMAL_MS       # gave up fast polling
    e.sync(store=store, rev=1, brightness=70)
    assert e.sync(store=store, rev=1, brightness=70)[1]["poll_ms"] == dl.POLL_NORMAL_MS
    e.dir.cleanup()


# ---- restart behaviour ------------------------------------------------------------------------

@case
def restart_keeps_desired_value_revision_store_and_the_real_report_age():
    e = Env()
    e.link.set_brightness(70)
    e.report_matching(app="0.7.0")
    store_before = e.link.store.desired()[0]
    e.clock.advance(100)
    link = e.restart()
    snap = link.snapshot()
    assert snap["brightness"]["desired"] == 70 and snap["brightness"]["rev"] == 1 and snap["brightness"]["store"] == store_before
    assert snap["device"]["contact"] == "gap" and 99 < snap["device"]["age_s"] < 101   # old report, honest age
    assert snap["device"]["report"]["app"] == "0.7.0"
    e.clock.advance(400)
    assert e.link.snapshot()["device"]["contact"] == "stale"
    e.dir.cleanup()


@case
def restart_never_shows_a_persisted_report_as_fresh():
    e = Env()
    e.sync()
    e.clock.advance(60)
    link = e.restart()
    assert link.snapshot()["device"]["contact"] != "fresh"
    e.dir.cleanup()


@case
def stick_with_an_old_revision_is_brought_up_to_date_after_a_restart():
    e = Env()
    e.link.set_brightness(70)
    store, _, _, _ = e.link.store.desired()
    e.sync(store=store, rev=1, brightness=70)
    e.restart()
    e.link.set_brightness(20)
    status, data = e.sync(store=store, rev=1, brightness=70)
    assert data["rev"] == 2 and data["settings"] == {"brightness": 20}
    e.dir.cleanup()


@case
def report_writes_are_throttled_but_changes_are_saved():
    e = Env()
    e.sync(uptime=10, rssi=-50)
    first = e.link.store.load_report()[0]
    e.clock.advance(5)
    e.sync(uptime=15, rssi=-52)                                               # only volatile fields changed
    assert e.link.store.load_report()[0] == first
    e.clock.advance(5)
    e.sync(uptime=20, brightness=40)                                          # a real change is saved at once
    assert e.link.store.load_report()[0] == e.clock()
    e.clock.advance(dl.PERSIST_EVERY_S + 1)
    e.sync(uptime=99, brightness=40)
    assert e.link.store.load_report()[0] == e.clock()
    e.dir.cleanup()


@case
def notifications_ride_on_the_sync_with_the_same_cursor():
    seen = []

    def notifications(after, ip):
        seen.append((after, ip))
        return {"id": 5, "kind": "turn_finished", "label": "x", "age": 0} if after < 5 else None

    e = Env(notifications=notifications)
    assert e.sync(notif=0)[1]["notification"]["id"] == 5
    assert e.sync(notif=5)[1]["notification"] is None
    assert seen == [(0, "10.0.0.12"), (5, "10.0.0.12")]
    e.dir.cleanup()



# ---- v0.8.0: volume, sleep settings, one-shot commands, capabilities ------------------------------

CAPS = ["brightness", "volume", "sleep"]


@case
def new_report_fields_are_validated_and_whitelisted():
    ok = dl.clean_report({"proto": 1, "voice_volume": 0, "chime_volume": 27, "idle_timeout_s": 60, "sleep_mode": "off",
                          "screen": "dim", "caps": CAPS, "cmds": [[7, "executed"], [8, "expired"]], "evil": 1})
    assert ok["voice_volume"] == 0 and ok["chime_volume"] == 27 and ok["idle_timeout_s"] == 60
    assert ok["sleep_mode"] == "off" and ok["screen"] == "dim" and ok["caps"] == CAPS
    assert ok["cmds"] == [[7, "executed"], [8, "expired"]] and "evil" not in ok
    odd = dl.clean_report({"proto": 1, "sleep_mode": "nap", "screen": "sleeping", "caps": ["x" * 40, 5, "", "sleep"],
                           "cmds": [[1, "executed"], [2, "bogus"], "x", [3], [-1, "executed"][:2], [4, "ignored", 9]] })
    assert "sleep_mode" not in odd and "screen" not in odd
    assert odd["caps"] == ["x" * 16, "sleep"]
    assert odd["cmds"] == [[1, "executed"]]                                  # malformed entries are dropped one by one
    assert dl.clean_report({"proto": 1, "cmds": [[2 ** 70, "executed"], [5, "executed"]]})["cmds"] == [[5, "executed"]]
    for bad in ({"proto": 1, "voice_volume": 101}, {"proto": 1, "voice_volume": True}, {"proto": 1, "idle_timeout_s": -1}):
        try:
            dl.clean_report(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert len(dl.clean_report({"proto": 1, "cmds": [[i, "executed"] for i in range(50)]})["cmds"]) == 8


@case
def every_setting_confirms_independently_against_the_same_revision():
    e = Env()
    e.link.set_setting("voice_volume", 0)                                    # mute
    e.link.set_setting("chime_volume", 20)
    e.link.set_setting("idle_timeout_s", 60)
    e.link.set_setting("sleep_mode", "off")
    store, rev, desired, _ = e.link.store.desired()
    status, answer = e.sync(store="", rev=0, boot="b1", caps=CAPS)
    assert answer["settings"] == desired and answer["rev"] == rev == 4
    controls = e.link.snapshot()["settings"]
    assert all(c["state"] == "pending" for k, c in controls.items() if c["desired"] is not None)
    assert controls["brightness"]["state"] == "unset"
    e.sync(store=store, rev=rev, boot="b1", caps=CAPS, voice_volume=0, chime_volume=20, idle_timeout_s=60, sleep_mode="off")
    controls = e.link.snapshot()["settings"]
    assert {k: c["state"] for k, c in controls.items()} == {
        "brightness": "unset", "voice_volume": "confirmed", "chime_volume": "confirmed",
        "idle_timeout_s": "confirmed", "sleep_mode": "confirmed"}              # 0 (mute) is a real, confirmable value
    e.sync(store=store, rev=rev, boot="b1", caps=CAPS, voice_volume=0, chime_volume=33, idle_timeout_s=60, sleep_mode="off")
    states = {k: c["state"] for k, c in e.link.snapshot()["settings"].items()}
    assert states["chime_volume"] == "differs" and states["voice_volume"] == "confirmed"
    e.dir.cleanup()


@case
def invalid_settings_are_rejected_by_the_link():
    e = Env()
    for key, bad in (("voice_volume", 101), ("chime_volume", -1), ("idle_timeout_s", 45), ("sleep_mode", "nap"),
                     ("nope", 1), ("brightness", 4)):
        try:
            e.link.set_setting(key, bad)
        except ValueError:
            continue
        raise AssertionError((key, bad))
    assert e.link.store.desired()[1] == 0
    e.dir.cleanup()


@case
def sleep_command_is_delivered_with_a_ttl_and_finishes_only_on_the_sticks_report():
    e = Env()
    e.sync(boot="b1", caps=CAPS)
    snap = e.link.command("sleep")
    assert snap["command"]["type"] == "sleep" and snap["command"]["state"] == "queued" and snap["command"]["ttl_left_s"] == dl.COMMAND_TTL_S
    status, answer = e.sync(boot="b1", caps=CAPS)
    cmd = answer["commands"][0]
    assert cmd["type"] == "sleep" and 40000 < cmd["ttl_ms"] <= dl.COMMAND_TTL_S * 1000 and answer["poll_ms"] == dl.POLL_FAST_MS
    assert e.link.snapshot()["command"]["state"] == "sent"                    # delivered is not executed
    e.clock.advance(2)
    assert e.sync(boot="b1", caps=CAPS)[1]["commands"][0]["id"] == cmd["id"]   # sent again until it is acknowledged
    e.sync(boot="b1", caps=CAPS, cmds=[[cmd["id"], "executed"]])
    snap = e.link.snapshot()
    assert snap["command"]["state"] == "executed" and snap["command"]["ttl_left_s"] is None
    assert e.sync(boot="b1", caps=CAPS, cmds=[[cmd["id"], "executed"]])[1]["commands"] == []   # repeats change nothing
    assert e.link.snapshot()["command"]["state"] == "executed"
    e.dir.cleanup()


@case
def expired_commands_are_never_delivered_after_reconnect():
    e = Env()
    e.link.command("sleep")
    e.clock.advance(dl.COMMAND_TTL_S + 1)                                     # the Stick was away for longer than the expiry
    status, answer = e.sync(boot="b1", caps=CAPS)
    assert answer["commands"] == []
    snap = e.link.snapshot()
    assert snap["command"]["state"] == "expired" and snap["command"]["detail"] == "not executed in time"
    e.dir.cleanup()


@case
def a_command_for_a_stick_without_the_sleep_capability_waits_and_expires_honestly():
    e = Env()
    e.link.command("sleep")
    for reported in ({}, {"caps": ["brightness"]}):                           # v0.7.0 reports no caps at all
        assert e.sync(boot="b1", **reported)[1]["commands"] == []
    assert e.link.snapshot()["caps"] == ["brightness"]
    e.clock.advance(dl.COMMAND_TTL_S + 1)
    assert e.link.snapshot()["command"]["state"] == "expired"
    e.dir.cleanup()


@case
def capabilities_are_none_until_the_first_report():
    e = Env()
    assert e.link.snapshot()["caps"] is None and e.link.snapshot()["screen"] is None
    e.sync(boot="b1", caps=CAPS, screen="dim")
    snap = e.link.snapshot()
    assert snap["caps"] == CAPS and snap["screen"] == "dim"
    e.dir.cleanup()


@case
def a_restarted_stick_never_receives_a_command_that_was_sent_to_its_previous_boot():
    e = Env()
    e.link.command("sleep")
    cmd = e.sync(boot="bootA", caps=CAPS)[1]["commands"][0]                  # delivered to boot A
    status, answer = e.sync(boot="bootB", caps=CAPS)                         # the Stick rebooted before running it
    assert answer["commands"] == []
    snap = e.link.snapshot()["command"]
    assert snap["state"] == "failed" and snap["detail"] == "the Stick restarted"
    e.clock.advance(1)
    assert e.sync(boot="bootB", caps=CAPS)[1]["commands"] == []              # and it is not replayed later
    e.dir.cleanup()


@case
def a_new_command_replaces_the_unfinished_one():
    e = Env()
    e.link.command("sleep")
    first = e.sync(boot="b1", caps=CAPS)[1]["commands"][0]
    e.link.command("wake")
    second = e.sync(boot="b1", caps=CAPS)[1]["commands"][0]
    assert second["type"] == "wake" and second["id"] > first["id"]
    e.sync(boot="b1", caps=CAPS, cmds=[[first["id"], "executed"]])           # a stale acknowledgement cannot resurrect it
    assert e.link.snapshot()["command"]["id"] == second["id"] and e.link.snapshot()["command"]["state"] == "sent"
    try:
        e.link.command("reboot")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown command accepted")
    e.dir.cleanup()


@case
def a_late_report_still_records_that_the_stick_executed_the_command():
    e = Env()
    e.link.command("sleep")
    cmd = e.sync(boot="b1", caps=CAPS)[1]["commands"][0]
    e.clock.advance(dl.COMMAND_TTL_S + 10)
    assert e.link.snapshot()["command"]["state"] == "expired"                # server-side view while the report is missing
    e.sync(boot="b1", caps=CAPS, cmds=[[cmd["id"], "executed"]])
    assert e.link.snapshot()["command"]["state"] == "executed"
    e.dir.cleanup()


@case
def the_stick_can_report_that_it_expired_or_ignored_a_command():
    for result in ("expired", "ignored"):
        e = Env()
        e.link.command("sleep")
        cmd = e.sync(boot="b1", caps=CAPS)[1]["commands"][0]
        e.sync(boot="b1", caps=CAPS, cmds=[[cmd["id"], result]])
        assert e.link.snapshot()["command"]["state"] == result
        e.dir.cleanup()


@case
def settings_and_commands_persist_across_a_server_restart_but_expire_on_time():
    e = Env()
    e.link.set_setting("voice_volume", 40)
    e.link.command("sleep")
    e.clock.advance(5)
    link = e.restart()
    snap = link.snapshot()
    assert snap["settings"]["voice_volume"]["desired"] == 40
    assert snap["command"]["state"] == "queued" and 39 < snap["command"]["ttl_left_s"] < 41
    e.clock.advance(dl.COMMAND_TTL_S)
    assert e.link.snapshot()["command"]["state"] == "expired"
    e.dir.cleanup()


@case
def the_v070_snapshot_fields_are_unchanged():
    e = Env()
    snap = e.link.snapshot()
    assert set(snap["brightness"]) == {"min", "max", "desired", "reported", "state", "rev", "store", "pending_s", "pending_note_s"}
    assert set(snap["device"]) == {"contact", "age_s", "report", "voice_job_active"}
    e.dir.cleanup()


# ---- reminders (v0.9.0) -----------------------------------------------------------------------------------

CAPS_M3 = ["brightness", "volume", "sleep", "reminder"]


def remind(e, minutes=1, mode="chime", text="Call Dan"):
    return e.link.reminder("reminders", {"text": text, "mode": mode, "when": {"kind": "in", "minutes": minutes}})["reminders"]["pending"][-1]


def sync_m3(e, boot="b1", **extra):
    store, rev, settings, _ = e.link.store.desired()
    return e.sync(store=store, rev=rev, boot=boot, caps=CAPS_M3, **extra)[1]


@case
def a_reminder_is_delivered_in_the_sync_reply_only_to_a_stick_that_can_handle_it():
    e = Env()
    item = remind(e)
    e.clock.advance(61)
    old = e.sync(boot="b1")[1]                                                      # a v0.8.0/v0.7.0 Stick: no "reminder" capability
    assert old["reminder"] is None and e.link.reminders.get(item["id"])["status"] == "scheduled"
    caps_without = e.sync(boot="b1", caps=["brightness", "volume", "sleep"])[1]
    assert caps_without["reminder"] is None and e.link.reminders.get(item["id"])["status"] == "scheduled"
    reply = sync_m3(e)
    assert reply["reminder"]["occ"] == item["occ"] and reply["reminder"]["mode"] == "chime"
    assert reply["reminder"]["label"] == "Call Dan" and reply["poll_ms"] == dl.POLL_FAST_MS
    assert e.link.reminders.get(item["id"])["status"] == "sent"
    e.dir.cleanup()


@case
def a_reminder_never_shares_a_reply_with_a_claude_notification():
    queued = [{"id": 5, "kind": "done", "label": "x"}]
    e = Env(notifications=lambda after, ip: queued[0] if after < 5 else None)
    item = remind(e)
    e.clock.advance(61)
    first = sync_m3(e, notif=0)
    assert first["notification"]["id"] == 5 and first["reminder"] is None
    second = sync_m3(e, notif=5)                                                    # the notification is acknowledged
    assert second["notification"] is None and second["reminder"]["occ"] == item["occ"]
    e.dir.cleanup()


@case
def reminder_results_in_a_sync_are_applied_before_the_next_one_is_chosen():
    e = Env()
    a, b = remind(e, 1, text="a"), remind(e, 1, text="b")
    e.clock.advance(61)
    first = sync_m3(e)["reminder"]
    assert first["label"] == "a"
    e.clock.advance(1)
    assert sync_m3(e, rems=[[first["occ"], "completed"]])["reminder"] is None       # the quiet gap, not a second alert at once
    e.clock.advance(20)
    assert sync_m3(e, rems=[[first["occ"], "completed"]])["reminder"]["label"] == "b"   # the result is re-reported: idempotent
    assert e.link.reminders.get(a["id"])["status"] == "completed" and b
    e.dir.cleanup()


@case
def a_garbled_reminder_result_is_dropped_on_its_own():
    report = dl.clean_report({"proto": 1, "rems": [[1, "completed"], ["x", "completed"], [2, "weird"], [3], "bad", [4, "dismissed"]]})
    assert report["rems"] == [[1, "completed"], [4, "dismissed"]]
    assert "rems" not in dl.clean_report({"proto": 1})


@case
def a_restart_with_an_unreported_reminder_is_redelivered_once_through_the_link():
    e = Env()
    item = remind(e)
    e.clock.advance(61)
    first = sync_m3(e, boot="b1")["reminder"]
    e.clock.advance(5)
    redelivered = sync_m3(e, boot="b2")["reminder"]                                 # the Stick came back with a new boot id
    assert redelivered["occ"] == first["occ"] and e.link.reminders.get(item["id"])["attempts"] == 2
    e.clock.advance(5)
    sync_m3(e, boot="b3")
    assert e.link.reminders.get(item["id"])["status"] == "failed"
    e.dir.cleanup()


@case
def the_stick_state_report_and_media_endpoint_need_the_device_key():
    e = Env(key=KEY)
    item = remind(e)
    body = json.dumps({"occ": item["occ"], "state": "received", "boot": "b1"}).encode()
    assert e.link.reminder_state("wrong", body, "10.0.0.12")[0] == 401
    assert e.link.reminder_state("", body, "10.0.0.12")[0] == 401
    assert e.link.reminder_media("wrong", str(item["occ"]), "10.0.0.12")[0] == 401
    for bad in (b"{}", b"not json", json.dumps({"occ": 1, "state": "nope", "boot": "b"}).encode(),
                json.dumps({"occ": True, "state": "received", "boot": "b"}).encode()):
        assert e.link.reminder_state(KEY, bad, "10.0.0.12")[0] == 400
    assert e.link.reminder_media(KEY, "abc", "10.0.0.12")[0] == 400
    assert e.link.reminder_media(KEY, str(item["occ"]), "10.0.0.12")[0] == 404          # not delivered yet
    e.clock.advance(61)
    reply = sync_m3(e)["reminder"]
    status, data = e.link.reminder_state(KEY, json.dumps({"occ": reply["occ"], "state": "presented", "boot": "b1"}).encode(), "10.0.0.12")
    assert status == 200 and data == {"changed": True}
    assert e.link.reminders.get(item["id"])["status"] == "presented"
    e.dir.cleanup()


@case
def repeated_bad_keys_on_the_reminder_endpoints_are_rate_limited_like_the_sync():
    e = Env()
    for _ in range(dl.AUTH_FAILS_PER_MIN):
        e.link.reminder_media("bad", "1", "10.0.0.99")
    assert e.link.reminder_media(KEY, "1", "10.0.0.99")[0] == 429
    e.dir.cleanup()


@case
def the_snapshot_carries_the_reminder_lists_and_the_panel_actions_validate():
    e = Env()
    snap = e.link.reminder("reminders", {"text": "x", "mode": "chime", "when": {"kind": "in", "minutes": 3}, "client_key": "k1"})
    assert snap["reminders"]["pending"][0]["status"] == "scheduled" and snap["reminders"]["tz"] == "Asia/Jerusalem"
    assert e.link.reminder("preview", {"when": {"kind": "in", "minutes": 3}})["preview"]["tz"] == "Asia/Jerusalem"
    for bad in ({"text": "x", "mode": "chime"}, {"text": "", "mode": "chime", "when": {"kind": "in", "minutes": 3}}):
        try:
            e.link.reminder("reminders", bad)
            raise AssertionError("accepted")
        except ValueError:
            pass
    for bad in ({"id": True, "occ": 1}, {"id": 1}, {"id": "1", "occ": 2}):
        try:
            e.link.reminder("cancel", bad)
            raise AssertionError("accepted")
        except ValueError:
            pass
    e.dir.cleanup()


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
