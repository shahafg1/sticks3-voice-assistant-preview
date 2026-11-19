# Statick (סטטיק): visual prototype (for review only)

**Open it from Finder, without Terminal:** open the project folder → `design` → `statick-prototype` and **double-click `index.html`** (it opens in your normal browser; no server, no internet).
The review images and animations are in the `previews` folder next to it.

**It is a browser mockup.** It draws the character with the Stick's 135×240 coordinates, but it is **not running on the Stick** and nothing about the device's real appearance or performance has been verified.
It makes no network requests, stores nothing, and is not connected to the server, the Stick, your settings or the live panel (`panel/` is untouched). `stick/robot.py` is unchanged.

## What is here
| File | What it is |
|---|---|
| `index.html`, `prototype.css`, `prototype.js` | The review page: the character, **all 11 existing states** at native size and enlarged, a pixel inspector (rendered at exactly 135×240 without smoothing, then enlarged without blur), the proposed desktop-panel illustration, and integration notes. |
| `statick.js` | The character engine: `scene(state, ms)` returns plain primitives (polygons, rounded rectangles, circles, lines, text). No gradients, transparency or filters; rotations are pre-computed into points. Blinks and glances follow a fixed schedule, so any frame is reproducible (`?freeze=<ms>`). |
| `preview-sheet.html` | The generator behind the review images (`?states=...&t=...&scale=...`, `?strip=<state>&step=...&count=...`). |
| `previews/` | `states-native-1x.png`, `states-enlarged-2x.png`, one animated GIF per state, and the page at desktop, narrow (390 px) and reduced-motion. |

States (the Stick's own names, `robot.py`): idle (blinks, glances), ready, listening (red pulsing glasses frame, REC, progress), thinking, speaking, sleeping (dim, drooped quiff, breathing, z's), dizzy (tilted glasses, wobbling quiff, spiral eyes), attention (orange pulse, "!"), done, error, puzzled/no speech.
Additions on the page: `?state=` and `?pt=` (pixel inspector), `?scenario=awake|sleeping|waiting|unknown` (panel illustration), `?freeze=<ms>`, `?reduced=1`. With "reduce motion" enabled in macOS the animation does not start: static frames are shown and a button starts it.

## Design decisions
* Tall blond quiff with brighter tips and dark sides; **oversized thin-frame glasses** whose lenses are skin-coloured so the eyes and mouth stay readable; simple white eyes with one pupil and one glint; the **only gold is a 3-pixel earring** (kept because it stays visible at native size).
* In recording and attention states the **glasses frame changes colour** (red / orange) instead of the old teal head outline.
* The labels sit below the chin, a little lower than today's, so the taller hair does not collide with the text; the bottom bar and dots keep their positions.
* Original and cartoonish, not a likeness of the person in the reference photo, and no imitation of anyone's voice (the voice stays the current local Hebrew one).

## Panel illustration (proposal)
Same approved remote layout and controls; only the picture in the small display changes. The illustration reflects **only what the Stick reports** (awake / sleeping / waiting / unknown) and is never a live view: the "איור בלבד" caption stays, "unknown" is grey with no blinking, and confirmed / pending / stale / error states remain separate flat elements driven by reports, not by the drawing.

## Not done (by design, until you approve the look)
No change to `robot.py`, no on-device measurement of memory or frame rate, no integration into the panel. Open design decisions are tracked by the maintainers.
