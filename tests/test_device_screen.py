"""Tests for the v0.8.0 parts of stick/device.py: volumes, awake brightness, screen actions, caches, commands.
Run: python3 tests/test_device_screen.py
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "stick"))
sys.path.insert(0, str(ROOT / "tests"))
import device  # noqa: E402
import sleep as sl  # noqa: E402
from test_device_sync import Files, FakeLcd, KEY, Stub, drive, make_state, sync_ok  # noqa: E402


CASES = []


def case(fn):
    CASES.append(fn)
    return fn



# ---- volume ---------------------------------------------------------------------------------------

@case
def unset_volumes_keep_todays_exact_effective_values():
    state, _ = make_state()
    assert state.voice_pct is None and state.chime_pct is None
    assert state.voice_raw() == 255 == device.DEFAULT_VOICE_RAW                # answers and shake clips today
    assert state.chime_raw() == 70 == device.DEFAULT_CHIME_RAW                 # chimes today
    r = state.report(0, now_ms=1000)
    assert r["voice_volume"] == 100 and r["chime_volume"] == 27                # shown as percentages
    assert state.lcd.calls == []                                                # nothing is touched at startup


@case
def volumes_map_percent_to_raw_with_zero_as_mute():
    state, _ = make_state()
    state.apply("S", 1, {"voice_volume": 0, "chime_volume": 0})
    assert state.voice_raw() == 0 and state.chime_raw() == 0
    state.apply("S", 2, {"voice_volume": 100, "chime_volume": 20})
    assert state.voice_raw() == 255 and state.chime_raw() == device.pct_to_raw(20) == 51
    for p in range(0, 101):
        assert device.raw_to_pct(device.pct_to_raw(p)) == p, p                 # the confirmed percentage is exact
    r = state.report(0, now_ms=1000)
    assert r["voice_volume"] == 100 and r["chime_volume"] == 20


@case
def volume_settings_are_validated_and_independent():
    state, _ = make_state()
    state.apply("S", 1, {"voice_volume": 40})
    assert state.voice_pct == 40 and state.chime_pct is None                    # the chime stays at its default
    for bad in (101, -1, True, "50", 50.5, None, [5]):
        state.apply("S", state.rev + 1, {"voice_volume": bad, "chime_volume": bad})
    assert state.voice_pct == 40 and state.chime_pct is None


@case
def volume_changes_are_only_stored_never_applied_to_the_display():
    # DeviceState has no speaker at all: a volume is read (voice_raw/chime_raw) at the start of each playback.
    state, _ = make_state()
    state.apply("S", 1, {"voice_volume": 10, "chime_volume": 5})
    assert state.lcd.calls == [] and state.voice_raw() == device.pct_to_raw(10) and state.chime_raw() == device.pct_to_raw(5)


@case
def unrelated_setting_changes_do_not_rewrite_the_backlight():
    state, _ = make_state()
    state.apply("S", 1, {"brightness": 60})
    assert state.lcd.calls == [device.pct_to_raw(60)]
    state.apply("S", 2, {"brightness": 60, "voice_volume": 30})              # a volume change carries the same brightness
    state.apply("S", 3, {"brightness": 60, "idle_timeout_s": 60})
    assert state.lcd.calls == [device.pct_to_raw(60)]                         # no needless hardware write
    state.apply("S", 4, {"brightness": 61})
    assert state.lcd.calls[-1] == device.pct_to_raw(61) and len(state.lcd.calls) == 2
    state.screen_apply("dim", 0)
    state.screen_apply("wake", 1000)                                          # a wake always restores it, even if the value is the same
    assert state.lcd.calls[-1] == device.pct_to_raw(61) and len(state.lcd.calls) == 4


# ---- awake brightness is kept separately from the sleeping backlight -------------------------------

@case
def sleeping_dims_the_backlight_but_the_awake_brightness_is_preserved():
    state, _ = make_state()
    state.apply("S", 1, {"brightness": 60})
    assert state.lcd.raw == device.pct_to_raw(60) and state.awake_pct == 60
    state.screen_apply("sleep", 0)                                              # the face is shown; no hardware change
    assert state.lcd.raw == device.pct_to_raw(60) and state.sleeper.state == sl.FACE
    state.screen_apply("dim", 3000)
    assert state.lcd.raw == device.pct_to_raw(device.DIM_PCT) and state.awake_pct == 60
    state.screen_apply("wake", 9000)
    assert state.lcd.raw == device.pct_to_raw(60) and state.sleeper.state == sl.AWAKE


@case
def off_mode_turns_the_backlight_fully_off_and_wake_restores_it():
    state, _ = make_state()
    state.apply("S", 1, {"brightness": 80})
    state.screen_apply("sleep", 0)
    state.screen_apply("off", 3000)
    assert state.lcd.raw == 0 and state.sleeper.state == sl.OFF
    state.screen_apply("wake", 9000)
    assert state.lcd.raw == device.pct_to_raw(80)


@case
def a_brightness_change_while_asleep_waits_for_the_wake():
    state, _ = make_state()
    state.apply("S", 1, {"brightness": 60})
    state.screen_apply("sleep", 0)
    state.screen_apply("dim", 3000)
    dim_raw = state.lcd.raw
    state.apply("S", 2, {"brightness": 90})                                     # the panel changes it during sleep
    assert state.lcd.raw == dim_raw and state.awake_pct == 90                   # the screen stays dim
    state.screen_apply("wake", 9000)
    assert state.lcd.raw == device.pct_to_raw(90)                               # and wakes at the new value


@case
def reports_show_the_awake_setting_while_asleep_so_brightness_stays_confirmed():
    state, _ = make_state()
    state.apply("S", 1, {"brightness": 60})
    assert state.report(0, now_ms=1000)["brightness"] == 60
    state.screen_apply("sleep", 0)
    state.screen_apply("dim", 3000)
    r = state.report(0, now_ms=5000)
    assert r["brightness"] == 60 and r["screen"] == "dim"                       # not the dim backlight value
    assert state.brightness_pct() == 60


@case
def the_cache_stores_the_awake_brightness_never_the_sleeping_one():
    state, files = make_state()
    state.apply("S", 1, {"brightness": 70})
    state.screen_apply("sleep", 0)
    state.screen_apply("dim", 3000)
    state.maybe_save(5000)
    cache = json.loads(files.writes[-1][1])
    assert cache["brightness"] == 70 and "screen" not in cache and "state" not in cache


@case
def boot_is_always_awake_with_the_saved_awake_brightness():
    state, files = make_state()
    state.apply("S", 1, {"brightness": 70, "voice_volume": 30, "chime_volume": 0, "idle_timeout_s": 60, "sleep_mode": "off"})
    state.screen_apply("sleep", 0)
    state.screen_apply("off", 3000)                                             # asleep with the backlight off...
    state.maybe_save(5000)
    lcd = FakeLcd(raw=0)                                                        # ...then the power is cycled
    rebooted, _ = make_state(files, lcd=lcd)
    assert rebooted.sleeper.state == sl.AWAKE and not rebooted.sleeper.asleep
    assert lcd.raw == device.pct_to_raw(70)                                     # the backlight comes back at the awake value
    assert (rebooted.voice_pct, rebooted.chime_pct) == (30, 0)
    assert (rebooted.sleeper.timeout_s, rebooted.sleeper.mode) == (60, "off")
    assert rebooted.sleeper.cmd is None and rebooted.sleeper.results == []


# ---- cache compatibility with v0.7.0 ----------------------------------------------------------------

@case
def a_v070_cache_still_loads_and_leaves_the_new_settings_at_their_defaults():
    old_cache = json.dumps({"store": "S9", "rev": 5, "brightness": 30})
    state, _ = make_state(Files(**{device.CACHE_FILE: old_cache}))
    assert (state.store, state.rev) == ("S9", 5) and state.lcd.raw == device.pct_to_raw(30)
    assert state.voice_pct is None and state.chime_pct is None
    assert state.sleeper.timeout_s == 0 and state.sleeper.mode == "dim"


@case
def invalid_new_cache_fields_are_ignored_individually():
    cache = {"store": "S", "rev": 1, "brightness": 40, "voice_volume": 500, "chime_volume": "x",
             "idle_timeout_s": 45, "sleep_mode": "nap"}
    state, _ = make_state(Files(**{device.CACHE_FILE: json.dumps(cache)}))
    assert state.awake_pct == 40
    assert state.voice_pct is None and state.chime_pct is None and state.sleeper.timeout_s == 0 and state.sleeper.mode == "dim"



# ---- commands over the sync ---------------------------------------------------------------------------

def sync_with(stub, **kw):
    state, files = make_state()
    p = device.DeviceSync(state, "127.0.0.1", stub.port, interval_ms=30, key=KEY)
    return p, state


@case
def sleep_and_wake_commands_reach_the_sleeper_with_a_ttl():
    stub = Stub({("POST", "/device/sync"): lambda r: (200, {"store": "S", "rev": 1, "settings": {}, "notification": None,
                                                            "poll_ms": 1000, "commands": [{"id": 12, "type": "sleep", "ttl_ms": 30000}]})})
    p, state = sync_with(stub)
    drive(p, 0.6)
    stub.close()
    assert state.sleeper.cmd is not None and state.sleeper.cmd[0] == 12 and state.sleeper.cmd[1] == "sleep"
    assert state.sleeper.results == []                                          # received, not executed
    assert state.sleeper.step(device.ticks_ms()) == "sleep"


@case
def duplicate_and_malformed_commands_in_the_reply_do_nothing_harmful():
    commands = [{"id": 5, "type": "sleep", "ttl_ms": 30000}, {"id": 5, "type": "sleep", "ttl_ms": 30000},
                {"id": "x", "type": "sleep", "ttl_ms": 30000}, {"id": 6, "type": "reboot", "ttl_ms": 30000},
                {"id": 7, "type": "sleep"}, "junk", None, {"id": 8, "type": "wake", "ttl_ms": 0}]
    stub = Stub({("POST", "/device/sync"): lambda r: (200, {"store": "S", "rev": 1, "settings": {}, "notification": None,
                                                            "poll_ms": 1000, "commands": commands})})
    p, state = sync_with(stub)
    drive(p, 0.8)
    stub.close()
    assert state.sleeper.cmd[0] == 5 and state.sleeper.seen == 5 and state.sleeper.results == []


@case
def a_v070_server_reply_without_commands_is_fine():
    stub = Stub({("POST", "/device/sync"): sync_ok(store="S", rev=1, settings={"brightness": 50})})
    p, state = sync_with(stub)
    drive(p, 0.6)
    stub.close()
    assert state.sleeper.cmd is None and state.store == "S"


@case
def results_are_reported_in_the_next_sync_after_execution():
    seen = []

    def reply(request):
        seen.append(json.loads(request["body"]))
        return 200, {"store": "S", "rev": 1, "settings": {}, "notification": None, "poll_ms": 1000,
                     "commands": [{"id": 3, "type": "sleep", "ttl_ms": 30000}] if len(seen) == 1 else []}

    stub = Stub({("POST", "/device/sync"): reply})
    p, state = sync_with(stub)
    end = time.monotonic() + 2.0
    done = False
    while time.monotonic() < end and len(seen) < 4:
        p.step()
        if not done and state.sleeper.cmd is not None:
            action = state.sleeper.step(device.ticks_ms())
            if action:
                state.screen_apply(action, device.ticks_ms())                  # the main loop carries it out
                done = True
        time.sleep(0.01)
    stub.close()
    assert done and seen[0]["cmds"] == [] and seen[0]["screen"] == "awake"
    last = seen[-1]
    assert last["cmds"] == [[3, "executed"]] and last["screen"] == "face"      # reported only after it was performed


@case
def sync_keeps_working_and_reports_while_the_screen_sleeps():
    stub = Stub({("POST", "/device/sync"): sync_ok(store="S", rev=2, settings={"voice_volume": 15, "brightness": 85})})
    p, state = sync_with(stub)
    state.apply("S", 1, {"brightness": 50})
    state.screen_apply("sleep", 0)
    state.screen_apply("off", 3000)
    lcd_raw = state.lcd.raw
    notes, slowest = drive(p, 1.0)
    stub.close()
    assert stub.requests, "no sync while the screen is off"
    assert json.loads(stub.requests[0]["body"])["screen"] == "off"
    assert state.voice_pct == 15 and state.awake_pct == 85                       # settings still apply
    assert state.lcd.raw == lcd_raw == 0                                         # without waking the screen
    assert slowest < 25, slowest


@case
def idle_timeout_and_mode_arrive_through_settings_and_are_reported_back():
    stub = Stub({("POST", "/device/sync"): sync_ok(store="S", rev=3, settings={"idle_timeout_s": 300, "sleep_mode": "off"})})
    p, state = sync_with(stub)
    drive(p, 0.8)
    stub.close()
    assert (state.sleeper.timeout_s, state.sleeper.mode) == (300, "off")
    r = state.report(0, now_ms=2000)
    assert r["idle_timeout_s"] == 300 and r["sleep_mode"] == "off"


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__, flush=True)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:300], flush=True)
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:300], flush=True)
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
