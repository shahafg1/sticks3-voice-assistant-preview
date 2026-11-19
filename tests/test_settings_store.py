"""Tests for settings_store.py. Run: python3 tests/test_settings_store.py"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import settings_store as ss  # noqa: E402


def fresh():
    d = tempfile.TemporaryDirectory()
    return d, Path(d.name) / "data" / "panel.db"


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def new_store_starts_empty_with_a_stable_id():
    d, path = fresh()
    s = ss.Store(path)
    store_id, rev, settings, updated = s.desired()
    assert rev == 0 and settings == {} and updated is None and len(store_id) == 16
    assert ss.Store(path).desired()[0] == store_id          # reopening keeps the same store id
    d.cleanup()


@case
def brightness_is_validated():
    d, path = fresh()
    s = ss.Store(path)
    for bad in (4, 101, 0, -1, True, False, "70", 70.0, 70.5, None, [], {}):
        try:
            s.set_brightness(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert s.desired()[1] == 0                                # failed attempts changed nothing
    assert s.set_brightness(5)[1] and s.set_brightness(100)[1]
    d.cleanup()


@case
def revision_increases_once_per_real_change():
    d, path = fresh()
    s = ss.Store(path)
    assert s.set_brightness(70) == (1, True)
    assert s.set_brightness(70) == (1, False)                 # same value: no change, no new revision
    assert s.set_brightness(55) == (2, True)
    store_id, rev, settings, updated = s.desired()
    assert rev == 2 and settings == {"brightness": 55} and updated is not None
    d.cleanup()


@case
def desired_state_survives_reopen():
    d, path = fresh()
    ss.Store(path).set_brightness(42)
    store_id, rev, settings, _ = ss.Store(path).desired()
    assert (rev, settings) == (1, {"brightness": 42})
    d.cleanup()


@case
def concurrent_writers_stay_consistent():
    d, path = fresh()
    s = ss.Store(path)
    errors = []

    def writer(index):
        try:
            base = 5 + 12 * index                              # disjoint value sets: every call is a real change
            for k in range(12):
                s.set_brightness(base + k)
                s.desired()
        except Exception as error:                              # noqa: BLE001
            errors.append(repr(error))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors[:3]
    _, rev, settings, _ = s.desired()
    assert rev == 96 and 5 <= settings["brightness"] <= 100, (rev, settings)
    d.cleanup()


@case
def committed_change_survives_a_hard_kill():
    d, path = fresh()
    ss.Store(path)
    code = ("import os, sys; sys.path.insert(0, %r); import settings_store as ss; s = ss.Store(%r); "
            "s.set_brightness(33); os._exit(9)" % (str(ROOT), str(path)))
    assert subprocess.run([sys.executable, "-c", code]).returncode == 9
    assert ss.Store(path).desired()[1:3] == (1, {"brightness": 33})
    d.cleanup()


@case
def interrupted_write_leaves_no_half_change():
    # A transaction that fails after writing the value but before the revision must roll back as a whole.
    d, path = fresh()
    s = ss.Store(path)
    s.set_brightness(60)
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE settings SET value = '99' WHERE key = 'brightness'")
    con.execute("ROLLBACK")                                     # what happens if the process dies before COMMIT
    con.close()
    assert s.desired()[1:3] == (1, {"brightness": 60})
    d.cleanup()


@case
def report_round_trip_and_overwrite():
    d, path = fresh()
    s = ss.Store(path)
    assert s.load_report() is None
    s.save_report({"app": "a", "rev": 1}, 100.5)
    s.save_report({"app": "b", "rev": 2}, 200.5)
    assert s.load_report() == (200.5, {"app": "b", "rev": 2})
    d.cleanup()


@case
def corrupt_database_is_reported_not_crashed():
    d, path = fresh()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is definitely not an sqlite database" * 50)
    try:
        ss.Store(path)
    except ss.StoreError as error:
        assert "cannot open" in str(error)
        d.cleanup()
        return
    raise AssertionError("corrupt file was accepted")


@case
def newer_schema_is_refused_so_a_rollback_never_misreads_it():
    d, path = fresh()
    ss.Store(path)
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 99")
    con.close()
    try:
        ss.Store(path)
    except ss.StoreError as error:
        assert "newer" in str(error)
        d.cleanup()
        return
    raise AssertionError("newer schema was accepted")


@case
def files_are_private():
    d, path = fresh()
    s = ss.Store(path)
    s.set_brightness(50)
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    d.cleanup()



# ---- v0.8.0: generic settings, commands, additive migration ---------------------------------------


@case
def every_setting_validates_exact_types_and_ranges():
    ok = {"brightness": (5, 100), "voice_volume": (0, 100), "chime_volume": (0, 100)}
    for key, (low, high) in ok.items():
        assert ss.validate_setting(key, low) == low and ss.validate_setting(key, high) == high
        for bad in (low - 1, high + 1, True, "50", 50.0, None, [50]):
            try:
                ss.validate_setting(key, bad)
            except ValueError:
                continue
            raise AssertionError((key, bad))
    for good in (0, 30, 60, 300):
        assert ss.validate_setting("idle_timeout_s", good) == good
    for bad in (1, 29, 301, -1, True, False, "30", 30.0, None):
        try:
            ss.validate_setting("idle_timeout_s", bad)
        except ValueError:
            continue
        raise AssertionError(("idle_timeout_s", bad))
    assert ss.validate_setting("sleep_mode", "dim") == "dim" and ss.validate_setting("sleep_mode", "off") == "off"
    for bad in ("DIM", "on", "", 0, None, True):
        try:
            ss.validate_setting("sleep_mode", bad)
        except ValueError:
            continue
        raise AssertionError(("sleep_mode", bad))
    try:
        ss.validate_setting("volume", 5)
    except ValueError:
        return
    raise AssertionError("unknown setting accepted")


@case
def settings_are_independent_and_share_one_revision_counter():
    d, path = fresh()
    s = ss.Store(path)
    assert s.set_setting("voice_volume", 0) == (1, True)                    # mute is a real value
    assert s.set_setting("chime_volume", 20) == (2, True)
    assert s.set_setting("voice_volume", 0) == (2, False)
    assert s.set_setting("idle_timeout_s", 60) == (3, True)
    assert s.set_setting("sleep_mode", "off") == (4, True)
    assert s.set_brightness(55) == (5, True)
    _, rev, settings, _ = s.desired()
    assert rev == 5 and settings == {"voice_volume": 0, "chime_volume": 20, "idle_timeout_s": 60,
                                     "sleep_mode": "off", "brightness": 55}
    assert set(s.setting_times()) == set(settings)
    d.cleanup()


@case
def fresh_database_is_created_at_the_current_minor_without_a_backup():
    d, path = fresh()
    ss.Store(path)
    con = sqlite3.connect(path)
    assert con.execute("SELECT value FROM meta WHERE key='schema_minor'").fetchone()[0] == str(ss.SCHEMA_MINOR)
    assert con.execute("SELECT name FROM sqlite_master WHERE name='commands'").fetchone()
    con.close()
    assert not list(path.parent.glob("*.bak"))
    d.cleanup()




@case
def command_lifecycle_pending_delivered_executed():
    d, path = fresh()
    s = ss.Store(path)
    cid = s.add_command("sleep", 45, now=1000.0)
    c = s.next_command(1001.0)
    assert c["id"] == cid and c["status"] == "pending" and c["type"] == "sleep" and c["expires_at"] == 1045.0
    assert s.finish_command(cid, "executed", 1002.0) is False                 # never sent: cannot have run
    s.mark_delivered(cid, 1003.0, "boot1")
    c = s.next_command(1004.0)
    assert c["status"] == "delivered" and c["delivered_boot"] == "boot1"
    assert s.finish_command(cid, "executed", 1005.0) is True
    assert s.next_command(1006.0) is None
    assert s.latest_command(1006.0)["status"] == "executed"
    d.cleanup()


@case
def commands_expire_and_are_never_delivered_late():
    d, path = fresh()
    s = ss.Store(path)
    cid = s.add_command("wake", 10, now=1000.0)
    assert s.next_command(1009.9) is not None
    assert s.next_command(1010.0) is None
    latest = s.latest_command(1010.0)
    assert latest["status"] == "expired" and latest["detail"] == "not executed in time"
    s.mark_delivered(cid, 1011.0, "b")                                         # an expired command cannot be delivered
    assert s.next_command(1011.0) is None
    d.cleanup()


@case
def a_late_acknowledgement_corrects_a_server_side_expiry():
    d, path = fresh()
    s = ss.Store(path)
    cid = s.add_command("sleep", 10, now=1000.0)
    s.mark_delivered(cid, 1001.0, "b")
    assert s.latest_command(1020.0)["status"] == "expired"                    # the report never arrived in time
    assert s.finish_command(cid, "executed", 1021.0) is True                  # but the Stick says it did run
    assert s.latest_command(1021.0)["status"] == "executed"
    # a result is final: the Stick re-reports recent results every sync, and repeats must change nothing
    assert s.finish_command(cid, "executed", 1022.0) is False
    assert s.finish_command(cid, "expired", 1023.0) is False and s.latest_command(1023.0)["status"] == "executed"
    assert s.finish_command(cid, "bogus", 1024.0) is False
    d.cleanup()


@case
def a_new_command_supersedes_the_unfinished_one():
    d, path = fresh()
    s = ss.Store(path)
    first = s.add_command("sleep", 45, now=1000.0)
    s.mark_delivered(first, 1001.0, "b")
    second = s.add_command("wake", 45, now=1002.0)
    assert second > first
    assert s.next_command(1003.0)["id"] == second
    assert s.finish_command(first, "executed", 1004.0) is False               # superseded commands do not finish
    d.cleanup()


@case
def commands_delivered_to_an_earlier_boot_are_failed_not_replayed():
    d, path = fresh()
    s = ss.Store(path)
    cid = s.add_command("sleep", 45, now=1000.0)
    s.mark_delivered(cid, 1001.0, "boot1")
    assert s.fail_other_boot("boot1", 1002.0) == 0                            # same boot: untouched
    assert s.fail_other_boot("boot2", 1003.0) == 1
    assert s.next_command(1004.0) is None
    latest = s.latest_command(1004.0)
    assert latest["status"] == "failed" and latest["detail"] == "the Stick restarted"
    d.cleanup()


@case
def command_ids_always_increase_and_kinds_are_validated():
    d, path = fresh()
    s = ss.Store(path)
    ids = [s.add_command("sleep", 45, now=1000.0) for _ in range(5)]          # same millisecond
    assert ids == sorted(set(ids))
    for bad in ("reboot", "", None, "SLEEP"):
        try:
            s.add_command(bad, 45)
        except ValueError:
            continue
        raise AssertionError(bad)
    d.cleanup()


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:300])
        except Exception as error:                               # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:300])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
