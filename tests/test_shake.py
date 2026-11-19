"""Tests for stick/shake.py with simulated accelerometer motion (values in g).

Run: python3 tests/test_shake.py
Samples arrive every 20-45 ms, like the Stick's idle loop (20 ms sleep plus an
occasional ~22 ms frame redraw).
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stick"))
import shake  # noqa: E402

GRAVITY = (0.0, 0.99, 0.0)


def timeline(seconds, start=0, seed=0):
    rng = random.Random(seed)
    t = start
    while t < start + seconds * 1000:
        yield t
        t += rng.choice((20, 21, 22, 42, 45))


def noise(rng, g=0.004):
    return tuple(rng.uniform(-g, g) for _ in range(3))


def run(signal, seconds, detector=None, start=0, seed=1):
    """Feed signal(t_ms) -> (x, y, z) to a detector; return trigger times."""
    rng = random.Random(seed)
    det = detector or shake.ShakeDetector()
    hits = []
    for t in timeline(seconds, start, seed):
        x, y, z = signal(t)
        n = noise(rng)
        if det.feed((x + n[0], y + n[1], z + n[2]), t):
            hits.append(t)
    return hits, det


def at_rest(t):
    return GRAVITY


def shaking(freq_hz, peak_g, axis=0, t0=0, t1=10 ** 9):
    def signal(t):
        a = list(GRAVITY)
        if t0 <= t < t1:
            a[axis] += peak_g * math.sin(2 * math.pi * freq_hz * (t - t0) / 1000)
        return tuple(a)
    return signal


def pickup(t):
    # lift over ~300 ms: smooth up-then-down acceleration of 0.5 g, plus a
    # slight tilt toward the user afterwards
    a = list(GRAVITY)
    if 1000 <= t < 1300:
        a[1] += 0.5 * math.sin(2 * math.pi * (t - 1000) / 300)
    if t >= 1300:
        tilt = min(1.0, (t - 1300) / 600) * math.radians(35)
        a = [0.0, 0.99 * math.cos(tilt), 0.99 * math.sin(tilt)]
    return tuple(a)


def snatch(t):
    # a quick, jerky grab: one sharp 1.5 g jolt in one direction, then still
    a = list(GRAVITY)
    if 1000 <= t < 1040:
        a[0] += 1.5
    return tuple(a)


def button_press(t):
    # thumb press: two small knocks (press and release) 300 ms apart
    a = list(GRAVITY)
    if 1000 <= t < 1025 or 1300 <= t < 1325:
        a[2] += 0.35
    return tuple(a)


def slow_tilt(t):
    ang = min(1.0, t / 1500) * math.radians(90)
    return (0.99 * math.sin(ang), 0.99 * math.cos(ang), 0.0)


def speaker_buzz(t):
    # loud playback vibration: small, fast jitter
    rng = random.Random(t)
    return tuple(g + rng.uniform(-0.06, 0.06) for g in GRAVITY)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def rest_never_triggers():
    hits, _ = run(at_rest, 30)
    assert hits == [], hits


@case
def pickup_and_tilt_do_not_trigger():
    hits, _ = run(pickup, 5)
    assert hits == [], hits


@case
def single_sharp_jolt_does_not_trigger():
    hits, _ = run(snatch, 5)
    assert hits == [], hits


@case
def button_press_does_not_trigger():
    hits, _ = run(button_press, 5)
    assert hits == [], hits


@case
def slow_tilt_does_not_trigger():
    hits, _ = run(slow_tilt, 5)
    assert hits == [], hits


@case
def speaker_vibration_does_not_trigger():
    hits, _ = run(speaker_buzz, 10)
    assert hits == [], hits


@case
def gentle_shake_triggers_once():
    # ~2 g at 4 Hz for 1.5 s: a gentle, deliberate shake
    hits, _ = run(shaking(4, 2.0, t0=1000, t1=2500), 6)
    assert len(hits) == 1 and 1000 < hits[0] < 2500, hits


@case
def vigorous_shake_triggers_once():
    hits, _ = run(shaking(6, 3.5, axis=2, t0=500, t1=2000), 6)
    assert len(hits) == 1, hits


@case
def very_short_shake_does_not_trigger():
    # one quick back-and-forth (~0.3 s) is not a deliberate shake
    hits, _ = run(shaking(4, 2.0, t0=1000, t1=1300), 4)
    assert hits == [], hits


@case
def continuous_shaking_does_not_repeat():
    # 10 s of nonstop shaking: one reaction, no rearm until it is still
    hits, _ = run(shaking(4, 2.0, t0=0, t1=10000), 12)
    assert len(hits) == 1, hits


@case
def rearms_after_cooldown_and_settling():
    # shake, stop for 4 s (cooldown 3 s + settle 0.8 s), shake again -> 2 hits
    def two_shakes(t):
        if t < 2000:
            return shaking(4, 2.0, t0=0, t1=2000)(t)
        if 6500 <= t < 8500:
            return shaking(4, 2.0, t0=6500, t1=8500)(t)
        return GRAVITY
    hits, _ = run(two_shakes, 10)
    assert len(hits) == 2 and hits[1] >= 6500, hits


@case
def no_rearm_while_still_moving_after_cooldown():
    # shaking continues past the cooldown: still only one reaction
    hits, _ = run(shaking(5, 2.5, t0=0, t1=8000), 9)
    assert len(hits) == 1, hits


@case
def reset_discards_partial_shake():
    # a shake half-done before entering idle must not complete afterwards
    det = shake.ShakeDetector()
    sig = shaking(4, 2.0, t0=0, t1=600)
    for t in timeline(0.45):
        assert not det.feed(sig(t), t)
    det.reset(450)  # app returns to idle: forget history
    hits, _ = run(at_rest, 3, detector=det, start=460)
    assert hits == [], hits


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
