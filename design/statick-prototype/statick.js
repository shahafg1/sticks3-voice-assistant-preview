/* Statick: the character engine of the visual prototype (a BROWSER MOCKUP; nothing here runs on the Stick).
 *
 * scene(state, t, opts) is a pure function of the state name and the time in milliseconds: it returns a flat list of primitives
 * (polygons, rounded rectangles, circles, thick lines, text) in the Stick's 135x240 portrait coordinates. That is deliberate: every shape is something the
 * Stick's M5GFX canvas can already draw (fillTriangle/fillRoundRect/fillCircle/fillArc/drawLine), rotations are computed here into plain points, and
 * there are no gradients, no transparency and no filters. render() turns the list into SVG for the browser.
 * Blinks and glances follow a fixed schedule (not random) so any frame can be reproduced exactly (?freeze=<ms>).
 */
(function () {
  "use strict";
  const W = 135, H = 240;
  const P = {
    bg: "#000000", skin: "#F1C8A0", shade: "#D49F72", hair: "#2B2833", blond: "#E5B53A", blondHi: "#FFE07A", blondTip: "#FFF4B8",
    frame: "#D8DEE2", sclera: "#FFFFFF", iris: "#1E1C27", mouth: "#8B3A3A", tongue: "#E9798B", red: "#FF3B3B", orange: "#FF8A3D",
    teal: "#19F5E1", tealDim: "#0B5F58", white: "#FFFFFF", grey: "#8A9BA0", gold: "#E9B824", blush: "#E58C8C", dark: "#1E1C27", xeye: "#8E2A1A",
  };
  const STATES = ["idle", "ready", "listening", "thinking", "speaking", "sleeping", "dizzy", "attention", "done", "error", "puzzled"];

  // ---- geometry helpers -------------------------------------------------------------------------------------------------------------
  const rad = (d) => (d * Math.PI) / 180;
  function rot(pts, cx, cy, deg) {
    if (!deg) return pts;
    const c = Math.cos(rad(deg)), s = Math.sin(rad(deg));
    return pts.map(([x, y]) => [cx + (x - cx) * c - (y - cy) * s, cy + (x - cx) * s + (y - cy) * c]);
  }
  function rrectPts(x, y, w, h, r, seg = 5) {
    const pts = [], corners = [[x + w - r, y + r, -90], [x + w - r, y + h - r, 0], [x + r, y + h - r, 90], [x + r, y + r, 180]];
    for (const [cx, cy, a0] of corners) for (let i = 0; i <= seg; i++) pts.push([cx + r * Math.cos(rad(a0 + (90 * i) / seg)), cy + r * Math.sin(rad(a0 + (90 * i) / seg))]);
    return pts;
  }
  // an arc band, angles clockwise from 3 o'clock (the convention of the Stick's fillArc)
  function arcPts(cx, cy, r0, r1, a0, a1, seg = 10) {
    const out = [], inner = [];
    for (let i = 0; i <= seg; i++) {
      const a = rad(a0 + ((a1 - a0) * i) / seg);
      out.push([cx + r1 * Math.cos(a), cy + r1 * Math.sin(a)]);
      inner.push([cx + r0 * Math.cos(a), cy + r0 * Math.sin(a)]);
    }
    return out.concat(inner.reverse());
  }
  const poly = (pts, fill) => ({ k: "poly", pts, fill });
  const circle = (x, y, r, fill) => ({ k: "circle", x, y, r, fill });
  const rrect = (x, y, w, h, r, fill) => ({ k: "rrect", x, y, w, h, r, fill });
  const line = (x1, y1, x2, y2, w, fill) => ({ k: "line", x1, y1, x2, y2, w, fill });
  const text = (s, y, fill, size, x = W / 2) => ({ k: "text", s, x, y, fill, size });

  // ---- deterministic liveliness -----------------------------------------------------------------------------------------------------------
  const BLINK_GAPS = [3200, 4600, 3700, 5200, 3900];
  const BLINK_LEN = 130;
  function blinking(t) {
    const total = BLINK_GAPS.reduce((a, b) => a + b, 0), m = ((t % total) + total) % total;
    let start = 0;
    for (const gap of BLINK_GAPS) { start += gap; if (m >= start - BLINK_LEN && m < start) return true; }   // each blink closes the eyes for BLINK_LEN ms
    return false;
  }
  const GLANCES = [[0, 0], [4, 0], [0, 0], [-4, -2], [0, 0], [4, 2], [0, 0], [-4, 0]];
  const glance = (t) => GLANCES[Math.floor(t / 2300) % GLANCES.length];

  // ---- the character -----------------------------------------------------------------------------------------------------------------------
  // o: { dx, dy (whole head), quiff (degrees), quiffDrop, frame (colour), tilt (glasses degrees), gdx, gdy, tint(fn), droop }
  function head(items, o) {
    const tint = o.tint || ((c) => c);
    const dx = o.dx || 0, dy = o.dy || 0;
    const X = (v) => v + dx, Y = (v) => v + dy;
    const shift = (pts) => pts.map(([x, y]) => [x + dx, y + dy]);
    // ears, with the small gold stud (the only gold: three pixels, kept because it stays visible at native size)
    items.push(circle(X(20), Y(120), 8, tint(P.skin)), circle(X(115), Y(120), 8, tint(P.skin)));
    items.push(circle(X(16), Y(127), 3, tint(P.gold)));
    // dark hair: the sides and a cap around the forehead
    items.push(poly(shift([[21, 104], [20, 80], [26, 68], [40, 60], [94, 60], [109, 68], [115, 80], [114, 104], [108, 96], [106, 82], [98, 74], [36, 74], [29, 82], [27, 96]]), tint(P.hair)));
    // the face
    items.push(rrect(X(21), Y(70), 93, 94, 34, tint(P.skin)));
    items.push(poly(shift([[21, 100], [27, 96], [29, 82], [36, 74], [98, 74], [106, 82], [108, 96], [114, 100], [114, 70], [21, 70]]), tint(P.hair)));
    items.push(poly(shift([[36, 74], [98, 74], [92, 80], [42, 80]]), tint(P.shade)));          // a soft hairline shadow on the forehead
    // the quiff: tall, blond, with brighter tips; it pivots at its base and may droop
    const pivot = [67, 66];
    const base = [[34, 72], [30, 54], [37, 40], [45, 28], [51, 30], [55, 15], [62, 19], [68, 4], [76, 14], [84, 6], [90, 16], [98, 10], [101, 22], [108, 25], [106, 38], [111, 48], [103, 62], [100, 72], [88, 66], [74, 63], [58, 65], [46, 68]];
    const tips = [[45, 28], [51, 30], [55, 15], [62, 19], [68, 4], [76, 14], [84, 6], [90, 16], [98, 10], [101, 22], [108, 25], [106, 38], [96, 34], [88, 30], [78, 28], [66, 30], [54, 34], [42, 38]];
    const mid = [[42, 38], [54, 34], [66, 30], [78, 28], [88, 30], [96, 34], [106, 38], [111, 48], [103, 62], [96, 52], [84, 46], [70, 44], [56, 48], [44, 56], [34, 58], [37, 40]];
    const q = (pts) => shift(rot(pts, pivot[0], pivot[1], o.quiff || 0).map(([x, y]) => [x, y + (o.quiffDrop || 0) * Math.max(0, (66 - y) / 66)]));
    items.push(poly(q(base), tint(P.blond)), poly(q(mid), tint(P.blondHi)), poly(q(tips), tint(P.blondTip)));
    // nose shadow
    items.push(poly(shift([[63, 129], [71, 129], [67, 135]]), tint(P.shade)));
  }

  function glasses(items, o, eyes) {
    const tint = o.tint || ((c) => c);
    const dx = (o.dx || 0) + (o.gdx || 0), dy = (o.dy || 0) + (o.gdy || 0), tilt = o.tilt || 0, frame = tint(o.frame || P.frame);
    const cx = 67 + dx, cy = 105 + dy;
    const lens = (x) => ({ x: x + dx, y: 88 + dy, w: 40, h: 34, r: 12 });
    const frames = [];
    for (const x of [26, 69]) {
      const l = lens(x);
      const outer = rot(rrectPts(l.x - 3, l.y - 3, l.w + 6, l.h + 6, l.r + 3), cx, cy, tilt);
      const inner = rot(rrectPts(l.x, l.y, l.w, l.h, l.r), cx, cy, tilt);
      frames.push([outer, inner]);
    }
    for (const [outer] of frames) items.push(poly(outer, frame));
    for (const [, inner] of frames) items.push(poly(inner, tint(P.skin)));
    const eyeCentres = rot([[46 + dx, 105 + dy], [89 + dx, 105 + dy]], cx, cy, tilt);
    eyes(eyeCentres);
    // bridge and temples
    const b = rot([[66 + dx, 101 + dy], [70 + dx, 101 + dy]], cx, cy, tilt);
    items.push(line(b[0][0], b[0][1], b[1][0], b[1][1], 3, frame));
    const tl = rot([[23 + dx, 97 + dy], [16 + dx, 105 + dy]], cx, cy, tilt), tr = rot([[112 + dx, 97 + dy], [119 + dx, 105 + dy]], cx, cy, tilt);
    items.push(line(tl[0][0], tl[0][1], tl[1][0], tl[1][1], 2, frame), line(tr[0][0], tr[0][1], tr[1][0], tr[1][1], 2, frame));
    // two small glints on the glass, never on the eyes
    const g = rot([[32 + dx, 96 + dy], [35 + dx, 93 + dy], [75 + dx, 96 + dy], [78 + dx, 93 + dy]], cx, cy, tilt);
    items.push(line(g[0][0], g[0][1], g[1][0], g[1][1], 2, P.white), line(g[2][0], g[2][1], g[3][0], g[3][1], 2, P.white));
  }

  // an eye: sclera + pupil (looking by lx, ly) or a closed/special shape
  function eye(items, c, kind, o) {
    const [x, y] = c, tint = o.tint || ((v) => v);
    const ink = tint(P.dark);
    switch (kind.type) {
      case "open": {
        const w = kind.w || 22, h = kind.h || 24, lx = kind.lx || 0, ly = kind.ly || 0;
        items.push(rrect(x - w / 2, y - h / 2, w, h, Math.min(w, h) / 2, tint(P.sclera)));
        const px = Math.max(-(w / 2 - 6), Math.min(w / 2 - 6, lx)), py = Math.max(-(h / 2 - 6), Math.min(h / 2 - 6, ly));
        items.push(circle(x + px, y + py, 6, ink), circle(x + px - 2, y + py - 2, 1.8, tint(P.white)));
        break;
      }
      case "blink": items.push(rrect(x - 11, y - 2, 22, 4, 2, ink)); break;
      case "closed": items.push(poly(arcPts(x, y - 1, 7, 10, 25, 155), ink)); break;          // a sleepy U
      case "happy": items.push(poly(arcPts(x, y + 6, 7, 10, 180, 360), ink)); break;           // a ^
      case "x": for (const d of [-1, 0, 1]) items.push(line(x - 8 + d, y - 8, x + 8 + d, y + 8, 3, tint(P.xeye)), line(x + 8 + d, y - 8, x - 8 + d, y + 8, 3, tint(P.xeye))); break;
      case "spiral": {
        items.push(circle(x, y, 12, tint(P.sclera)));
        const s = (kind.phase || 0) * 45;
        items.push(poly(arcPts(x, y, 8, 10.5, s, s + 270), ink), poly(arcPts(x, y, 3, 5.5, s + 180, s + 450), ink));
        break;
      }
      case "half": { const w = 20, h = 12; items.push(rrect(x - w / 2, y - h / 2 + 2, w, h, 6, tint(P.sclera)), circle(x + (kind.lx || 0), y + 2, 5, ink)); break; }
    }
  }

  function brows(items, left, right, o) {            // left/right: [raise (px up), tilt (px: positive lifts the outer end)]
    const tint = o.tint || ((c) => c), dx = o.dx || 0, dy = o.dy || 0;
    for (const [x0, x1, [raise, tilt], outerLeft] of [[31, 59, left, true], [76, 104, right, false]]) {
      const y = 80 - raise + dy;
      const yo = y - tilt, yi = y + tilt;
      const a = outerLeft ? [x0 + dx, yo] : [x0 + dx, yi], b = outerLeft ? [x1 + dx, yi] : [x1 + dx, yo];
      items.push(line(a[0], a[1], b[0], b[1], 4, tint(P.hair)));
    }
  }

  function mouth(items, kind, o) {
    const tint = o.tint || ((c) => c), dx = o.dx || 0, dy = o.dy || 0, cx = 67 + dx, cy = 150 + dy + (kind.y || 0), ink = tint(P.mouth);
    switch (kind.type) {
      case "smile": items.push(poly(arcPts(cx, cy - 8, 11, 14, 28, 152), ink)); break;
      case "bigsmile": items.push(poly(arcPts(cx, cy - 10, 14, 18, 22, 158), ink)); break;
      case "flat": { const w = kind.w || 18; items.push(rrect(cx - w / 2 + (kind.dx || 0), cy + 1, w, 4, 2, ink)); break; }
      case "o": items.push(circle(cx, cy + 3, 6, ink), circle(cx, cy + 3, 3, tint(P.tongue))); break;
      case "open": {
        const h = Math.max(4, 4 + Math.round(kind.level * 18)), w = 26 + Math.round(kind.level * 4);
        items.push(rrect(cx - w / 2, cy + 3 - h / 2, w, h, Math.min(9, h / 2), ink));
        if (h > 12) items.push(rrect(cx - w / 2 + 5, cy + 3 + h / 2 - 7, w - 10, 6, 3, tint(P.tongue)));
        break;
      }
      case "wavy": {
        const amp = 2 + (kind.amp || 0);
        for (let i = 0; i < 4; i++) {
          const x0 = cx - 16 + i * 8, y0 = cy + 3 + (i % 2 ? amp : -amp);
          items.push(line(x0, y0, x0 + 8, cy + 3 + (i % 2 ? -amp : amp), 3, ink));
        }
        break;
      }
    }
  }

  const dimColor = (c) => {                // night palette for sleeping: every colour at about 45 percent, like the old robot's dim outline
    const n = parseInt(c.slice(1), 16), f = (v) => Math.round(v * 0.62);
    return "#" + [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => f(v).toString(16).padStart(2, "0")).join("");
  };
  const greyColor = (c) => {
    const n = parseInt(c.slice(1), 16), r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255, y = Math.round(0.3 * r + 0.59 * g + 0.11 * b);
    const h = y.toString(16).padStart(2, "0");
    return "#" + h + h + h;
  };

  // ---- the states (names are the Stick's own: robot.py) -----------------------------------------------------------------------------------------
  function scene(state, t, opts = {}) {
    const items = [], o = {}, label = opts.label, detail = opts.detail;
    const bl = blinking(t), [gx, gy] = glance(t);
    const sway = Math.sin(t / 1500) * 1.5;
    const openEye = (extra) => ({ type: bl ? "blink" : "open", ...extra });
    let eyes, lb = [0, 0], rb = [0, 0], mo = { type: "smile" }, extra = null;
    switch (state) {
      case "idle":
        o.quiff = sway; eyes = [openEye({ lx: gx, ly: gy }), openEye({ lx: gx, ly: gy })];
        lb = [0, 0]; rb = [0, 0]; mo = { type: "smile" };
        extra = () => items.push(text(label || "Press button", 198, P.grey, 14));
        break;
      case "ready":
        o.quiff = sway; eyes = [openEye({ h: 28, w: 24 }), openEye({ h: 28, w: 24 })]; lb = [3, 0]; rb = [3, 0]; mo = { type: "flat", w: 12 };
        extra = () => items.push(text(label || "Get ready", 194, P.white, 18));
        break;
      case "listening": {
        const pulse = Math.floor(t / 500) % 2 === 0;
        o.frame = pulse ? P.red : P.frame; o.quiff = 2; eyes = [{ type: "open", h: 30, w: 26 }, { type: "open", h: 30, w: 26 }]; lb = [3, 0]; rb = [3, 0]; mo = { type: "o" };
        extra = () => {
          items.push(circle(14, 14, 5, pulse ? P.red : "#3a0f0f"), text("REC", 17, P.red, 12, 33));
          items.push(text(label || "Listening", 190, P.white, 18));
          const prog = Math.min(100, Math.floor(((t % 6000) * 100) / 6000)) / 4;
          items.push(rrect(17, 212, 101, 8, 4, P.tealDim));
          if (prog) items.push(rrect(17, 212, Math.max(8, (101 * prog) / 25), 8, 4, P.red));
        };
        break;
      }
      case "thinking": {
        const dot = Math.floor(t / 350) % 3, drift = [0, 2, 4, 2][Math.floor(t / 900) % 4];
        o.quiff = sway + 2; o.gdy = 1; eyes = [{ type: "open", lx: -3 + drift / 2, ly: -5, h: 20 }, { type: "open", lx: -3 + drift / 2, ly: -5, h: 20 }];
        lb = [4, -2]; rb = [0, 1]; mo = { type: "flat", w: 12, dx: 6 };
        extra = () => {
          items.push(text(label || "Thinking", 186, P.white, 18));
          if (opts.elapsed !== false) items.push(text(Math.floor(t / 1000) + " s", 206, P.grey, 14));
          for (let i = 0; i < 3; i++) items.push(circle(49 + i * 18, 224, 5, i === dot ? P.teal : P.tealDim));
        };
        break;
      }
      case "speaking": {
        const level = opts.level !== undefined ? opts.level : 0.5 + 0.5 * Math.abs(Math.sin(t / 130)) * Math.abs(Math.sin(t / 470 + 1));
        const bounce = Math.floor(t / 250) % 2;
        o.quiff = sway + (bounce ? 1 : 0); o.dy = bounce ? -1 : 0;
        eyes = [openEye({ w: 24, h: 24 }), openEye({ w: 24, h: 24 })]; lb = [bounce, 0]; rb = [bounce, 0]; mo = { type: "open", level };
        extra = () => items.push(text(label || "Speaking", 198, P.grey, 14));
        break;
      }
      case "sleeping": {
        const phase = Math.floor(t / 600) % 8, breath = [0, 1, 2, 2, 1, 0, 0, 0][phase];
        o.tint = dimColor; o.quiff = 10; o.quiffDrop = 8; o.dy = breath * 0.5; o.tilt = -3;
        eyes = [{ type: "closed" }, { type: "closed" }]; lb = [-1, 0]; rb = [-1, 0]; mo = { type: "flat", w: 8 + breath * 2 };
        extra = () => {
          for (let i = 0; i < Math.min(3, Math.floor(phase / 2)); i++) {
            const size = 8 + 3 * i, x0 = 96 + 5 * i, y0 = 50 - 14 * i;
            items.push(line(x0, y0, x0 + size, y0, 2, P.teal), line(x0 + size, y0, x0, y0 + size, 2, P.teal), line(x0, y0 + size, x0 + size, y0 + size, 2, P.teal));
          }
        };
        break;
      }
      case "dizzy": {
        const phase = Math.floor(t / 90) % 8, wob = Math.floor(t / 160) % 4, dxs = [0, 3, 0, -3][wob];
        o.dx = dxs; o.tilt = [-10, -4, -12, -6][wob]; o.gdx = 0; o.quiff = [-9, 7, -7, 9][wob];
        eyes = [{ type: "spiral", phase }, { type: "spiral", phase: (phase + 4) % 8 }]; lb = [2, 3]; rb = [2, -3]; mo = { type: "wavy", amp: Math.floor(t / 110) % 3 };
        extra = () => items.push(text(label || "Whoa!", 194, P.white, 18));
        break;
      }
      case "attention": {
        const pulse = Math.floor(t / 300) % 2 === 0;
        o.frame = pulse ? P.orange : P.frame; o.quiff = pulse ? 3 : -2; eyes = [{ type: "open", w: 26, h: 32 }, { type: "open", w: 26, h: 32 }]; lb = [4, 0]; rb = [4, 0]; mo = { type: "o" };
        extra = () => {
          items.push(rrect(120, 8, 9, 24, 4, P.orange), circle(124, 42, 5, P.orange));
          items.push(text(label || "Needs you", 190, P.white, 18), text(detail || "Check the Mac", 210, P.grey, 14));
        };
        break;
      }
      case "done": {
        const bob = Math.floor(t / 450) % 2;
        o.quiff = bob ? 2 : -1; eyes = [{ type: "happy" }, { type: "happy" }]; lb = [2, 0]; rb = [2, 0]; mo = { type: "bigsmile" };
        extra = () => { items.push(text(label || "Reply ready", 192, P.white, 18)); };
        o.blush = true;
        break;
      }
      case "puzzled": {
        const wobble = Math.floor(t / 400) % 2;
        o.quiff = wobble ? 4 : 1; o.tilt = wobble ? 2 : 0; eyes = [{ type: "open", h: 26 }, { type: "half", lx: 3 }]; lb = [3, 0]; rb = [-2, -2]; mo = { type: "flat", w: 14, dx: wobble ? 5 : 3 };
        extra = () => {
          items.push(text("?", 40 + (wobble ? 2 : 0), P.teal, 26, 120));
          items.push(text(label || "No speech", 190, P.white, 18), text(detail || "Try again", 210, P.grey, 14));
        };
        break;
      }
      case "error":
        o.frame = P.orange; o.quiff = -3; eyes = [{ type: "x" }, { type: "x" }]; lb = [-1, 3]; rb = [-1, -3]; mo = { type: "flat", w: 20 };
        extra = () => items.push(text(label || "Error", 190, P.orange, 18), text(detail || "Something failed", 210, P.grey, 14));
        break;
      default: throw new Error("unknown state " + state);
    }
    if (opts.tint) o.tint = opts.tint;
    head(items, o);
    brows(items, lb, rb, o);
    glasses(items, o, (centres) => { eye(items, centres[0], eyes[0], o); eye(items, centres[1], eyes[1], o); });
    if (o.blush) for (const x of [31, 104]) items.push(circle(x, 133, 5, P.blush));
    mouth(items, mo, o);
    if (extra && !opts.noLabels) extra();
    return items;
  }

  // ---- the desktop panel's illustration: only what the Stick REPORTS (awake / sleeping / waiting / unknown); never a listening or speaking face ----
  function panelScene(kind, t) {
    const tint = kind === "unknown" ? greyColor : undefined;
    if (kind === "sleeping") return scene("sleeping", t, { noLabels: true });
    if (kind === "waiting") {
      const items = scene("thinking", 0, { noLabels: true });
      for (let i = 0; i < 3; i++) items.push(circle(55 + i * 12.5, 184, 4.5, i === Math.floor(t / 400) % 3 ? P.teal : P.tealDim));
      return items;
    }
    if (kind === "unknown") {
      const items = scene("idle", 0, { noLabels: true, tint });
      return items;
    }
    return scene("idle", t, { noLabels: true });
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------------------------------
  const NS = "http://www.w3.org/2000/svg";
  function svgString(items, opts = {}) {
    const bg = opts.bg === undefined ? P.bg : opts.bg;
    const parts = [`<svg xmlns="${NS}" viewBox="0 0 ${W} ${H}" width="${opts.w || W}" height="${opts.h || H}" direction="ltr"${opts.crisp ? ' shape-rendering="crispEdges"' : ""}>`];
    if (bg) parts.push(`<rect width="${W}" height="${H}" fill="${bg}"/>`);
    for (const it of items) {
      if (it.k === "poly") parts.push(`<polygon points="${it.pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ")}" fill="${it.fill}"/>`);
      else if (it.k === "circle") parts.push(`<circle cx="${it.x.toFixed(1)}" cy="${it.y.toFixed(1)}" r="${it.r}" fill="${it.fill}"/>`);
      else if (it.k === "rrect") parts.push(`<rect x="${it.x.toFixed(1)}" y="${it.y.toFixed(1)}" width="${it.w}" height="${it.h}" rx="${Math.min(it.r, it.w / 2, it.h / 2)}" fill="${it.fill}"/>`);
      else if (it.k === "line") parts.push(`<line x1="${it.x1.toFixed(1)}" y1="${it.y1.toFixed(1)}" x2="${it.x2.toFixed(1)}" y2="${it.y2.toFixed(1)}" stroke="${it.fill}" stroke-width="${it.w}" stroke-linecap="round"/>`);
      else if (it.k === "text") parts.push(`<text x="${it.x}" y="${it.y}" fill="${it.fill}" font-size="${it.size}" font-family="Montserrat, system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif" text-anchor="middle">${String(it.s).replace(/&/g, "&amp;").replace(/</g, "&lt;")}</text>`);
    }
    parts.push("</svg>");
    return parts.join("");
  }

  window.Statick = { W, H, P, STATES, scene, panelScene, svgString, blinking, glance, primitives: (items) => items.length };
})();
