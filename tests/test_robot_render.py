"""Tests for the new character on the Stick (stick/robot.py + stick/statick_shapes.py), run on CPython with a FAKE canvas (tests/fake_canvas.py).

What this proves: the real robot.py code runs for every state, keeps the interface the rest of the app uses, stays inside the 135x240 canvas, draws once per
frame, only redraws when the picture changes, and its generated triangle data is current. What it does NOT prove: how it looks or performs on the real
Stick (Pillow is not the Stick's rasteriser; speed and memory are measured on the device by the maintainers).
Run: python3 tests/test_robot_render.py   (Pillow is optional: with it the fake canvas also rasterises the picture)
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import random
import subprocess
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "stick"))
sys.dont_write_bytecode = True
import fake_canvas as F  # noqa: E402

CASES = []
STATES = ["idle", "ready", "listening", "thinking", "speaking", "sleeping", "dizzy", "attention", "done", "error", "puzzled"]


def case(fn):
    CASES.append(fn)
    return fn


def with_robot(fn):
    def run():
        clock = F.Clock()
        _, restore = F.install(clock)
        sys.modules.pop("robot", None)
        sys.modules.pop("statick_shapes", None)
        try:
            import robot
            random.seed(7)
            fn(robot, clock)
        finally:
            restore()
            sys.modules.pop("robot", None)
    run.__name__ = fn.__name__
    return case(run)


def frame_calls(robot_obj):
    """The drawing calls of the most recent frame (from the last fillScreen)."""
    calls = robot_obj.c.calls
    start = max(i for i, c in enumerate(calls) if c[0] == "fillScreen")
    return calls[start:]


def inside(x, y, slack=1):
    return -slack <= x <= 135 + slack and -slack <= y <= 240 + slack


@with_robot
def the_interface_and_the_state_names_are_unchanged(robot, clock):
    r = robot.Robot()
    for name in ("set", "update", "relabel", "hold"):
        assert callable(getattr(r, name))
    old = (ROOT / "stick" / "voice_send.py").read_text() + (ROOT / "stick" / "main.py").read_text()
    used = set(re.findall(r'robot\.set\("([a-z]+)"', old))
    assert used and used <= set(STATES), used - set(STATES)                         # every state the app asks for is drawn
    for state in STATES:
        r.set(state, "Label", "Detail", duration_ms=6000 if state == "listening" else None, elapsed=True)
        assert r.state == state


@with_robot
def every_state_draws_a_complete_frame_inside_the_canvas_with_one_push(robot, clock):
    r = robot.Robot()
    for state in STATES:
        for dt in (0, 400, 900, 1700, 3100, 6100):
            r.set(state, duration_ms=6000 if state == "listening" else None, elapsed=True)
            clock.t += dt
            r.level = 0.9
            pushes = r.c.pushes
            r._key = None
            r.update(force=True)
            assert r.c.pushes == pushes + 1                                         # exactly one push per drawn frame
            calls = frame_calls(r)
            assert calls[0][0] == "fillScreen" and len(calls) < 260, (state, len(calls))
            for c in calls:
                name = c[0]
                if name == "fillTriangle":
                    assert all(inside(c[i], c[i + 1], 6) for i in (1, 3, 5)), (state, dt, c)
                elif name == "fillCircle":
                    assert inside(c[1] - c[3], c[2] - c[3], 1) and inside(c[1] + c[3], c[2] + c[3], 1), (state, dt, c)
                elif name == "fillRoundRect":
                    assert inside(c[1], c[2], 1) and inside(c[1] + c[3], c[2] + c[4], 1), (state, dt, c)
                elif name == "fillArc":
                    assert inside(c[1] - c[4], c[2] - c[4], 1) and inside(c[1] + c[4], c[2] + c[4], 1), (state, dt, c)
                elif name == "drawString":
                    assert 0 <= c[2] < 240 and c[3] >= 0, c


@with_robot
def labels_details_and_progress_behave_as_before(robot, clock):
    r = robot.Robot()
    r.set("idle")
    assert [t for t in r.c.texts if t[0] == "Press button"]
    r.set("error", "Error", "A very long detail that is cut")
    assert any(t[0] == "A very long deta" for t in r.c.texts)                         # cut to 16 characters, as before
    r.set("thinking", elapsed=True)
    clock.t += 3500
    r._key = None
    r.update(force=True)
    assert any(t[0] == "3 s" for t in r.c.texts)
    r.set("listening", duration_ms=6000)
    clock.t += 3000
    r._key = None
    r.update(force=True)
    bars = [c for c in frame_calls(r) if c[0] == "fillRoundRect" and c[2] == 212]
    assert len(bars) == 2 and bars[1][3] > 40                                         # the red progress bar filled about half
    r.relabel("Sending")
    assert any(t[0] == "Sending" for t in r.c.texts)


@with_robot
def it_redraws_only_when_the_picture_changes_and_never_faster_than_the_frame_rate(robot, clock):
    r = robot.Robot()
    r.set("sleeping")
    drawn = r.c.pushes
    clock.t += robot.FRAME_MS + 5
    r.update()                                                                        # nothing changed: no redraw
    assert r.c.pushes == drawn
    clock.t += 700
    r.update()                                                                        # the breathing phase moved: one redraw
    assert r.c.pushes == drawn + 1
    drawn = r.c.pushes
    for _ in range(5):                                                                # calls closer together than FRAME_MS are skipped
        clock.t += 5
        r.update()
    assert r.c.pushes == drawn
    r.set("speaking")
    drawn = r.c.pushes
    clock.t += 100
    r.update(level=0.9)                                                               # the mouth follows the level it is given
    assert r.c.pushes == drawn + 1
    mouth = [c for c in frame_calls(r) if c[0] == "fillRoundRect" and c[5] <= 9 and c[3] >= 26]
    assert mouth and mouth[0][4] > 12
    clock.t += 100
    r.update(level=0.0)
    mouth = [c for c in frame_calls(r) if c[0] == "fillRoundRect" and c[5] <= 9 and c[3] >= 26]
    assert mouth and mouth[0][4] <= 6


@with_robot
def hold_keeps_animating_for_the_requested_time(robot, clock):
    r = robot.Robot()
    r.set("idle")
    start = clock.t
    r.hold(500)
    assert 500 <= clock.t - start < 560


@with_robot
def the_dizzy_face_tilts_the_glasses_and_swings_the_quiff_while_idle_does_not(robot, clock):
    r = robot.Robot()
    r.set("dizzy")
    xs = set()
    for step in range(12):
        clock.t += 100
        r._key = None
        r.update(force=True)
        xs.add(tuple(c[1:7] for c in frame_calls(r) if c[0] == "fillTriangle")[40])     # a quiff triangle moves between frames
    assert len(xs) > 3
    r.set("idle")
    clock.t += 100
    r._key = None
    r.update(force=True)
    frame_tris = lambda: sum(1 for c in frame_calls(r) if c[0] == "fillTriangle" and c[7] == robot.FRAME_C)
    assert frame_tris() < 12                                                          # idle: plain rounded rectangles (only the bridge and temples are triangles)
    r.set("dizzy")
    clock.t += 100
    r._key = None
    r.update(force=True)
    assert frame_tris() > 30                                                          # dizzy: the tilted frames are fans of triangles


@case
def the_generated_triangle_data_is_current_and_covers_the_whole_polygons():
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_statick_shapes.py"), "--check"], capture_output=True, timeout=120)
    assert done.returncode == 0, "stick/statick_shapes.py is out of date: run tools/gen_statick_shapes.py"
    import importlib
    sys.path.insert(0, str(ROOT / "stick"))
    shapes = importlib.import_module("statick_shapes")
    for name in ("HAIR_CAP", "HAIR_SIDES", "HAIR_SHADE", "QUIFF_BASE", "QUIFF_MID", "QUIFF_TIPS", "NOSE"):
        pts, tris = getattr(shapes, name)
        assert pts and tris and all(len(p) == 2 and all(isinstance(v, int) for v in p) for p in pts), name
        assert all(len(t) == 3 and all(isinstance(i, int) and 0 <= i < len(pts) for i in t) for t in tris), name
        assert len(tris) == len(pts) - 2                                            # a simple polygon of n corners is n - 2 triangles


@case
def the_stick_code_needs_only_what_the_device_has():
    source = (ROOT / "stick" / "robot.py").read_text()
    for used in re.findall(r"\bc\.(\w+)\(", source):
        assert used in {"fillScreen", "fillTriangle", "fillRoundRect", "fillCircle", "fillArc", "drawLine", "fillRect", "push", "setFont", "setTextSize", "setTextColor", "textWidth", "drawString"}, used
    assert "fillPolygon" not in source                                              # the device has no polygon fill (probed on the Stick)
    assert (ROOT / "stick" / "statick_shapes.py").stat().st_size < 6000             # small enough to be a non-issue for flash and RAM


if __name__ == "__main__":
    failures = 0
    for fn in CASES:
        try:
            fn()
            print("ok   " + fn.__name__, flush=True)
        except Exception as error:                      # noqa: BLE001
            import traceback
            failures += 1
            print("FAIL %s: %s: %s" % (fn.__name__, type(error).__name__, error), flush=True)
            traceback.print_exc()
    print("%d/%d robot render cases passed" % (len(CASES) - failures, len(CASES)))
    sys.exit(1 if failures else 0)
