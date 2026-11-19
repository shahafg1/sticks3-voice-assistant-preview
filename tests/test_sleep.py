"""Tests for stick/sleep.py (the Sleeper state machine). Run: python3 tests/test_sleep.py"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stick"))
import sleep as sl  # noqa: E402

T0 = 100_000


def new(timeout=0, mode="dim"):
    s = sl.Sleeper(T0)
    s.configure(timeout, mode)
    return s


def do(s, now):
    """What the main loop does: ask, carry out, report. Returns the action."""
    action = s.step(now)
    if action:
        s.performed(action, now)
    return action


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---- idle timeout ----------------------------------------------------------------------------

@case
def automatic_sleep_is_disabled_by_default():
    s = sl.Sleeper(T0)
    assert s.timeout_s == 0 and s.mode == "dim" and s.state == sl.AWAKE and not s.asleep
    for hours in (1, 24, 24 * 30):
        assert s.step(T0 + hours * 3_600_000) is None


@case
def each_timeout_triggers_exactly_at_the_threshold():
    for timeout in (30, 60, 300):
        s = new(timeout)
        assert s.step(T0 + timeout * 1000 - 1) is None
        assert s.step(T0 + timeout * 1000) == "sleep"


@case
def activity_restarts_the_idle_timer():
    s = new(30)
    s.activity(T0 + 25_000)
    assert s.step(T0 + 30_000) is None and s.step(T0 + 54_999) is None
    assert s.step(T0 + 55_000) == "sleep"


@case
def stepping_and_reporting_never_reset_the_timer():
    s = new(30)
    for t in range(0, 29_000, 20):                       # the main loop polls every 20 ms, sync and panel included
        assert s.step(T0 + t) is None
        s.report_fields()
    assert s.last_activity == T0
    assert s.step(T0 + 30_000) == "sleep"


@case
def invalid_configuration_is_ignored():
    s = new(60, "off")
    for bad in (45, 1, -30, "30", None, True, 30.0 + 0.5):
        s.configure(timeout_s=bad)
    for bad in ("nap", "", None, 0, "DIM"):
        s.configure(mode=bad)
    assert s.timeout_s == 60 and s.mode == "off"
    s.configure(timeout_s=0)
    assert s.timeout_s == 0


# ---- sleep sequence and wake -------------------------------------------------------------------

@case
def sleep_shows_the_face_then_dims():
    s = new(30, "dim")
    assert do(s, T0 + 30_000) == "sleep" and s.state == sl.FACE and s.asleep
    assert s.step(T0 + 30_000 + sl.FACE_MS - 1) is None                       # the face stays up briefly
    assert do(s, T0 + 30_000 + sl.FACE_MS) == "dim" and s.state == sl.DIM and s.asleep
    assert s.step(T0 + 10_000_000) is None                                    # nothing repeats while asleep


@case
def off_mode_turns_the_backlight_off_after_the_face():
    s = new(30, "off")
    do(s, T0 + 30_000)
    assert do(s, T0 + 30_000 + sl.FACE_MS) == "off" and s.state == sl.OFF and s.asleep


@case
def mode_change_applies_to_the_next_transition():
    s = new(30, "dim")
    do(s, T0 + 30_000)
    s.configure(mode="off")                                                   # changed while the face is showing
    assert do(s, T0 + 30_000 + sl.FACE_MS) == "off"


@case
def wake_restores_awake_and_restarts_the_timer():
    s = new(30)
    do(s, T0 + 30_000); do(s, T0 + 40_000)
    assert s.state == sl.DIM
    s.performed("wake", T0 + 100_000)
    assert s.state == sl.AWAKE and not s.asleep
    assert s.step(T0 + 129_999) is None and s.step(T0 + 130_000) == "sleep"   # a fresh idle period starts at the wake


@case
def wake_from_the_face_phase_is_a_wake_too():
    s = new(30)
    do(s, T0 + 30_000)
    assert s.state == sl.FACE
    s.performed("wake", T0 + 31_000)
    assert s.state == sl.AWAKE and s.step(T0 + 31_000 + sl.FACE_MS) is None


# ---- one-shot commands ----------------------------------------------------------------------------

@case
def sleep_command_works_with_the_idle_timeout_disabled():
    s = new(0)
    assert s.submit(10, "sleep", 45_000, T0)
    assert s.results == []                                                    # receipt is not execution
    assert s.step(T0 + 100) == "sleep"
    assert s.results == []                                                    # deciding is not executing either
    s.performed("sleep", T0 + 100)
    assert s.results == [[10, "executed"]] and s.state == sl.FACE
    assert s.cmd is None


@case
def acknowledgement_appears_only_after_the_action_was_performed():
    s = new(0)
    s.submit(5, "sleep", 45_000, T0)
    assert s.step(T0) == "sleep"
    assert s.report_fields()["cmds"] == []
    s.performed("sleep", T0)
    assert s.report_fields()["cmds"] == [[5, "executed"]]


@case
def wake_command_wakes_a_sleeping_screen():
    s = new(0)
    do(s, T0) if s.submit(1, "sleep", 45_000, T0) else None
    do(s, T0 + sl.FACE_MS)
    assert s.state == sl.DIM
    assert s.submit(2, "wake", 45_000, T0 + 10_000)
    assert do(s, T0 + 10_000) == "wake" and s.state == sl.AWAKE
    assert s.results[-1] == [2, "executed"]


@case
def commands_that_change_nothing_are_acknowledged_without_acting():
    s = new(0)
    s.submit(1, "wake", 45_000, T0)                                           # already awake
    assert s.step(T0) is None and s.results == [[1, "executed"]]
    do(s, T0) if s.submit(2, "sleep", 45_000, T0) else None
    s.submit(3, "sleep", 45_000, T0 + 1)                                      # already asleep
    assert s.step(T0 + 1) is None and s.results[-1] == [3, "executed"]


@case
def a_command_waits_while_busy_but_only_within_its_expiry():
    s = new(0)
    s.submit(7, "sleep", 45_000, T0)
    # the Stick is busy for 30 s: step() is simply not called, then it is idle again within the expiry
    assert s.step(T0 + 30_000) == "sleep"
    s2 = new(0)
    s2.submit(8, "sleep", 45_000, T0)
    assert s2.step(T0 + 45_000) is None                                       # idle again, but too late
    assert s2.results == [[8, "expired"]] and s2.state == sl.AWAKE
    assert s2.step(T0 + 60_000) is None and s2.state == sl.AWAKE              # and it is never run afterwards


@case
def expired_commands_are_not_replayed_after_a_reconnect():
    s = new(0)
    s.submit(9, "sleep", 1_000, T0)
    assert s.step(T0 + 5_000) is None and s.results == [[9, "expired"]]
    assert s.submit(9, "sleep", 45_000, T0 + 6_000) is False                  # the server re-sending the same id: ignored
    assert s.step(T0 + 6_001) is None and s.state == sl.AWAKE


@case
def repeated_older_and_invalid_commands_are_ignored():
    s = new(0)
    assert s.submit(10, "sleep", 45_000, T0)
    assert s.submit(10, "sleep", 45_000, T0 + 100) is False                   # at-least-once delivery: duplicate
    assert s.submit(9, "wake", 45_000, T0 + 100) is False                     # older id
    for bad in (("reboot", 45_000), ("sleep", 0), ("sleep", -5), ("", 10), (None, 10)):
        assert s.submit(11, bad[0], bad[1], T0 + 100) is False
    assert s.cmd == [10, "sleep", T0 + 45_000]


@case
def a_newer_command_replaces_the_pending_one():
    s = new(0)
    s.submit(1, "sleep", 45_000, T0)
    assert s.submit(2, "wake", 45_000, T0 + 100)
    assert s.results == [[1, "ignored"]] and s.cmd[0] == 2
    assert s.step(T0 + 100) is None and s.results[-1] == [2, "executed"]      # wake while awake: nothing to do


@case
def a_local_wake_is_not_undone_by_an_old_manual_sleep():
    s = new(0)
    s.submit(1, "sleep", 45_000, T0)
    do(s, T0)
    do(s, T0 + sl.FACE_MS)                                                    # asleep, command executed
    s.performed("wake", T0 + 20_000)                                          # the user pressed the button
    for t in range(T0 + 20_000, T0 + 600_000, 5_000):
        assert s.step(t) is None, t                                           # and the device stays awake: no desired state
    assert s.state == sl.AWAKE


@case
def a_reboot_starts_awake_with_no_commands():
    s = new(60, "off")
    s.submit(1, "sleep", 45_000, T0)
    do(s, T0)
    fresh = sl.Sleeper(T0 + 1_000_000)                                        # what a restart creates
    assert fresh.state == sl.AWAKE and fresh.cmd is None and fresh.results == [] and fresh.timeout_s == 0


@case
def results_are_capped_and_reported_oldest_to_newest():
    s = new(0)
    for i in range(1, 9):
        s.submit(i, "wake", 45_000, T0 + i)
        s.step(T0 + i)
    assert s.results == [[i, "executed"] for i in range(5, 9)]
    fields = s.report_fields()
    assert fields["cmds"] == s.results and fields["cmds"] is not s.results
    assert set(fields) == {"screen", "idle_timeout_s", "sleep_mode", "cmds"}


@case
def timer_sleep_and_command_sleep_do_not_conflict():
    s = new(30)
    assert do(s, T0 + 30_000) == "sleep"
    s.submit(1, "sleep", 45_000, T0 + 31_000)                                 # a panel click while already sleeping
    assert s.step(T0 + 31_000) is None and s.results == [[1, "executed"]]
    assert s.state == sl.FACE


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
