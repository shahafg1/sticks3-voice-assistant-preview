"""Static checks for the panel's frontend files (no browser, no server). Run: python3 tests/test_panel_frontend.py

They protect the rules the panel must keep: strict CSP (no inline script or style), no network except the panel's own API, text inserted with
textContent only, no demo or prototype leftovers, every element id the scripts use exists, sliders increase left to right, history collapsed.
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRS = [d for d in (ROOT / "panel", ROOT / "panel-next") if (d / "index.html").exists()]
KNOWN_CAPS = {"brightness", "volume", "sleep", "reminder"}
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def each(fn):
    """Run a check for every panel folder present (panel/, and panel-next/ while a new UI is being prepared)."""
    def run():
        assert DIRS
        for d in DIRS:
            fn(d, (d / "index.html").read_text(), (d / "app.js").read_text(), (d / "reminders.js").read_text(), (d / "app.css").read_text())
    run.__name__ = fn.__name__
    CASES.append(run)
    return run


@each
def strict_csp_friendly_markup(d, html, app, rem, css):
    assert not re.search(r"\sstyle\s*=", html), f"{d.name}: inline style attribute (blocked by the panel's CSP)"
    assert "<style" not in html and not re.search(r"<script(?![^>]*\bsrc=)", html), f"{d.name}: inline script or style block"
    assert not re.search(r"\son[a-z]+\s*=", html), f"{d.name}: inline event handler"
    for text, name in ((app, "app.js"), (rem, "reminders.js")):
        assert 'setAttribute("style"' not in text and "cssText" not in text, f"{name}: sets an inline style attribute"
    assert html.count("__CSRF__") == 1 and 'lang="he" dir="rtl"' in html


@each
def no_dynamic_html_and_no_unsafe_apis(d, html, app, rem, css):
    for text, name in ((app, "app.js"), (rem, "reminders.js")):
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write", "localStorage", "sessionStorage",
                    "WebSocket", "XMLHttpRequest", "sendBeacon", "importScripts"):
            assert bad not in text, f"{d.name}/{name} uses {bad}"


@each
def only_the_panels_own_api_is_called(d, html, app, rem, css):
    assert not re.search(r"https?://", html + app + rem + css), f"{d.name}: external address"
    for text, name in ((app, "app.js"), (rem, "reminders.js")):
        for call in re.findall(r"fetch\(([^,)]+)", text):
            assert call.strip().startswith(('"/', "path", "`/")), f"{name}: fetch({call})"
        for path in re.findall(r'post\("(/[^"]*)"', text) + re.findall(r'api\("(/[^"]*)"', text):
            assert path.startswith("/api/"), f"{name}: unexpected endpoint {path}"


@each
def no_prototype_or_demo_leftovers(d, html, app, rem, css):
    blob = (html + app + rem + css).lower()
    for word in ("mock", "scenario", "prototype", "sandbox", "design review", "דוגמה", "אב-טיפוס", "לבדיקת העיצוב", "הדמיית"):
        assert word.lower() not in blob, f"{d.name}: leftover '{word}'"
    assert "Math.random" not in app + rem, "random values must never stand in for telemetry"


@each
def every_element_the_scripts_use_exists(d, html, app, rem, css):
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', app + rem)) | set(re.findall(r'getElementById\("([^"]+)"\)', app + rem))
    assert not (used - ids), f"{d.name}: ids used but missing in index.html: {sorted(used - ids)}"
    for form_id in ("rem-fold-0", "rem-fold-1"):
        assert form_id in ids


@each
def capability_gates_use_known_capabilities(d, html, app, rem, css):
    caps = set(re.findall(r'data-caps="([^"]+)"', html))
    assert caps <= KNOWN_CAPS and caps, caps
    assert 'snap.caps.includes("reminder")' in rem and "supports(" in app


@each
def approved_layout_rules_are_kept(d, html, app, rem, css):
    assert re.search(r'input\[type="range"\][^{]*\{[^}]*direction:\s*ltr', css), "sliders must keep increasing from left to right"
    assert re.search(r"<details[^>]*id=\"hist-drawer\"(?![^>]*\bopen\b)[^>]*>", html), "reminder history must be collapsed by default"
    assert 'role="tablist"' in html and 'id="panel-rem"' in html and 'id="panel-remote"' in html
    assert "prefers-reduced-motion" in css, "reduced-motion support"
    assert "outline: 3px solid var(--accent)" in css, "visible focus ring"
    assert "design-review" not in html and "review-body" not in html


@each
def telemetry_is_never_invented(d, html, app, rem, css):
    # values come from the snapshot, and absent ones show a dash; confirmation only from the server's own state
    assert '"—"' in app
    assert 'x.st = "ok"' not in app and 'state = "confirmed"' not in app
    assert 's.state === "confirmed"' in app


@each
def token_expiry_is_explained_and_never_replayed(d, html, app, rem, css):
    assert '"bad token"' in app and "refreshToken" in app and "token: true" in app
    assert "res.token" in rem and "RESTARTED" in rem
    assert "client_key" in rem and "crypto.randomUUID()" in rem
    send = app[app.index("async function send("):app.index("function renderControl")]
    assert "r.token" in send and "retryIn = null" in send            # a refused token never schedules an automatic resend


@case
def scripts_parse():
    node = shutil.which("node")
    assert node, "Node.js is needed for this check (tests/README.md, prerequisites); it is never skipped silently"
    for d in DIRS:
        for name in ("app.js", "reminders.js"):
            result = subprocess.run([node, "--check", str(d / name)], capture_output=True, text=True)
            assert result.returncode == 0, f"{d.name}/{name}: {result.stderr[:300]}"


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:400])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:300])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
