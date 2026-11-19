"""Static and geometric checks of the Statick visual prototype (design/statick-prototype/). No browser, no server, no network. Run: python3 tests/test_statick_prototype.py

What they protect: the prototype stays a self-contained browser mockup that says so, uses only primitives the Stick can draw, stays inside the 135x240
screen, is reproducible, handles reduced motion, keeps the panel illustration to what the Stick reports, and leaves the Stick's renderer and the live
panel's illustration untouched. They say NOTHING about how the character looks on the real device.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO = ROOT / "design" / "statick-prototype"
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def text(name):
    return (PROTO / name).read_text()


NODE = r"""
global.window = {}; require(process.argv[2]);
const S = window.Statick, out = {states: S.STATES, bad: [], kinds: new Set(), frames: 0, sameTwice: true};
const inb = (x, y) => x >= -0.5 && x <= 135.5 && y >= -0.5 && y <= 240.5;
for (const st of S.STATES) for (let t = 0; t <= 9000; t += 70) {
  const a = S.scene(st, t), b = S.scene(st, t);
  if (JSON.stringify(a) !== JSON.stringify(b)) out.sameTwice = false;
  out.frames++;
  for (const it of a) {
    out.kinds.add(it.k);
    if (it.k === "poly") { for (const [x, y] of it.pts) if (!inb(x, y)) out.bad.push([st, t, it.k, x, y]); }
    else if (it.k === "circle") { if (!inb(it.x - it.r, it.y - it.r) || !inb(it.x + it.r, it.y + it.r)) out.bad.push([st, t, it.k, it.x, it.y]); }
    else if (it.k === "rrect") { if (!inb(it.x, it.y) || !inb(it.x + it.w, it.y + it.h)) out.bad.push([st, t, it.k, it.x, it.y]); }
    else if (it.k === "line") { if (!inb(it.x1, it.y1) || !inb(it.x2, it.y2)) out.bad.push([st, t, it.k, it.x1, it.y1]); }
    for (const v of Object.values(it)) if (typeof v === "string" && v.startsWith("#") && !/^#[0-9a-fA-F]{6}$/.test(v)) out.bad.push([st, t, "colour", v]);
  }
}
const panel = {};
for (const k of ["awake", "sleeping", "waiting", "unknown"]) panel[k] = S.panelScene(k, 700).filter((i) => i.k === "text").map((i) => i.s);
out.panelText = panel; out.kinds = [...out.kinds];
console.log(JSON.stringify(out));
"""


def run_node():
    node = shutil.which("node")
    assert node, "node is needed for the geometry check"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "check.js"
        script.write_text(NODE)
        done = subprocess.run([node, str(script), str(PROTO / "statick.js")], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@case
def the_prototype_is_a_self_contained_page_that_says_it_is_a_browser_mockup():
    html, js, css, engine = text("index.html"), text("prototype.js"), text("prototype.css"), text("statick.js")
    assert "מוקאפ בדפדפן" in html and "לא רץ על ה-Stick" in html and "Browser mockup" in html
    for name in ("index.html", "prototype.js", "prototype.css", "statick.js"):
        body = text(name)
        for bad in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "localStorage", "sessionStorage", "indexedDB", "eval(", "new Function"):
            assert bad not in body, f"{name} uses {bad}"
        assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", body), f"{name} refers to an external address"
    assert 'src="statick.js"' in html and 'src="prototype.js"' in html and 'href="prototype.css"' in html          # opens from Finder: relative files only
    assert not (PROTO / "package.json").exists()


@case
def every_existing_stick_state_is_covered_with_the_sticks_own_names():
    robot = (ROOT / "stick" / "robot.py").read_text()
    device_states = set(re.findall(r'if s == "([a-z]+)"', robot)) | set(re.findall(r'elif s == "([a-z]+)"', robot)) | {"error"}      # the last branch of _draw is the error face
    result = run_node()
    assert set(result["states"]) == device_states, (set(result["states"]) ^ device_states)
    assert len(result["states"]) == 11


@case
def the_character_uses_only_simple_primitives_stays_on_the_135_by_240_screen_and_is_reproducible():
    result = run_node()
    assert set(result["kinds"]) <= {"poly", "circle", "rrect", "line", "text"}, result["kinds"]
    assert result["bad"] == [], result["bad"][:5]                               # no point outside the screen in any state, at any of ~130 moments each; plain #rrggbb colours only
    assert result["sameTwice"] and result["frames"] > 1000
    engine = text("statick.js")
    for forbidden in ("linearGradient", "radialGradient", "<filter", "filter=", "feGaussian", "opacity", "rgba", "blur", "clipPath"):
        assert forbidden not in engine, f"statick.js uses {forbidden}, which the Stick cannot draw"


@case
def reduced_motion_is_honoured_in_the_page_and_in_the_stylesheet():
    js, css = text("prototype.js"), text("prototype.css")
    assert "prefers-reduced-motion: reduce" in js and "prefers-reduced-motion: reduce" in css
    assert "userPlayed" in js and "הפעלת אנימציה" in js                          # off until the person starts it
    assert "FRAME_MS = 60" in js and "FRAME_MS = 60" in (ROOT / "stick" / "robot.py").read_text()   # the preview redraws no faster than the Stick does


@case
def the_panel_illustration_shows_only_what_the_stick_reports_and_never_claims_to_be_live():
    html, js, engine = text("index.html"), text("prototype.js"), text("statick.js")
    body = engine[engine.index("function panelScene"):engine.index("// ---- rendering")]
    for used in re.findall(r'scene\("([a-z]+)"', body):
        assert used in ("sleeping", "thinking", "idle"), used                    # awake / sleeping / waiting / unknown only: never listening, speaking, dizzy...
    assert "איור בלבד" in html and "אינו שידור חי" in html and "הפאנל החי לא שונה" in html
    result = run_node()
    assert all("REC" not in t and "Speaking" not in t for kind in result["panelText"].values() for t in kind)   # no device labels in the illustration
    for kind in ("awake", "sleeping", "waiting", "unknown"):
        assert f'data-k="{kind}"' in html


@case
def the_panel_keeps_its_illustration_contract_with_the_new_character():
    now = (ROOT / "panel" / "index.html").read_text()
    drawing = now[now.index('<svg class="robot"'):now.index("</svg>", now.index('<g class="unknown-mark">'))]
    assert 'id="robot"' in drawing and 'data-expr="unknown"' in drawing and 'class="quiff"' in drawing and 'class="sleep-eyes"' in drawing and 'class="dots"' in drawing
    for expr in ("awake", "sleeping", "waiting", "unknown"):
        assert expr in drawing                                                   # every report-derived expression has its own parts
    assert "איור בלבד" in now and "אינו שידור חי" in now                     # still an illustration, never a mirror of the Stick's screen
    css = (ROOT / "panel" / "app.css").read_text()
    assert "grayscale" in css and 'data-screen="unknown"' in css            # unknown stays visibly unknown
    assert "prefers-reduced-motion: no-preference" in css and "animation: sway" in css   # the quiff's sway is inside the existing reduced-motion guard


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
    print("%d/%d prototype cases passed" % (len(CASES) - failures, len(CASES)))
    sys.exit(1 if failures else 0)
