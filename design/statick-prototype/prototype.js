/* Page logic for the Statick visual prototype (a browser mockup; no network, no storage, nothing leaves the page). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const q = new URLSearchParams(location.search);
  const FRAME_MS = 60;                                           // the Stick redraws at most every 60 ms (robot.py FRAME_MS)
  const reducedQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const forceReduced = q.get("reduced") === "1";
  const freeze = q.get("freeze") === null ? null : Number(q.get("freeze"));
  const INFO = {
    idle: ["מנוחה", "הברירת מחדל. מצמוץ וגם הצצות הצידה מדי פעם; הקוויף מתנדנד מעט.", 1000],
    ready: ["מוכן להקלטה", "השנייה אחרי הלחיצה, לפני שההקלטה מתחילה. עיניים רחבות וגבות מורמות.", 1000],
    listening: ["מקשיב", "בזמן הקלטה: מסגרת המשקפיים פועמת באדום, נקודת REC ופס התקדמות.", 100],
    thinking: ["חושב", "מחכה לתשובה: מבט למעלה, גבה מורמת, שלוש נקודות וספירת שניות.", 400],
    speaking: ["מדבר", "הפה נפתח לפי רמת קול. ב-Stick הרמה נגזרת מהצליל; בהשמעה ב-Mac זה דפוס כללי, לא סנכרון.", 130],
    sleeping: ["ישן", "עיניים עצומות, נשימה איטית, Z מרחפים, צבעים עמומים והקוויף שמוט.", 2600],
    dizzy: ["מסוחרר (ניעור)", "תגובת הניעור: משקפיים נטויים, קוויף מתנדנד, עיני ספירלה ופה גלי.", 100],
    attention: ["דורש תשומת לב", "התראה או תזכורת: מסגרת כתומה פועמת וסימן קריאה.", 100],
    done: ["מוכן (התראת סיום)", "תשובה או משימה הסתיימה: עיני ^ ,לחיים סמוקות וחיוך גדול.", 100],
    error: ["שגיאה", "עיני X, מסגרת כתומה ופה שטוח. הטקסט מראה את הסיבה.", 0],
    puzzled: ["לא זוהה דיבור", "עין אחת גדולה ואחת חצי עצומה, סימן שאלה. נאמר לנסות שוב.", 100],
  };
  const reduced = () => forceReduced || reducedQuery.matches;
  let playing = !reduced() && freeze === null, speed = 1, base = 0, started = performance.now(), userPlayed = false;
  const clock = () => (freeze !== null ? freeze : base + (playing ? (performance.now() - started) * speed : 0));

  // ---- the gallery ---------------------------------------------------------------------------------------------------------------------------------
  const tiles = [];
  for (const state of Statick.STATES) {
    const [name, desc] = INFO[state];
    const el = document.createElement("article"); el.className = "tile";
    el.innerHTML = `<h3>${name} <code dir="ltr">${state}</code></h3><p>${desc}</p><div class="views"><figure class="device"><div class="screen native" role="img" aria-label="${name}, גודל מקורי"></div><figcaption>מקורי <bdi dir="ltr">135×240</bdi></figcaption></figure><figure class="device"><div class="screen zoom" role="img" aria-label="${name}, מוגדל"></div><figcaption>מוגדל</figcaption></figure></div>`;
    $("grid").appendChild(el);
    tiles.push({ state, native: el.querySelector(".native"), zoom: el.querySelector(".zoom"), last: -1 });
  }
  const timeFor = (state) => (reduced() && !userPlayed && freeze === null ? INFO[state][2] : clock());
  const frameOf = (t) => Math.floor(t / FRAME_MS);
  function drawTile(tile) {
    const t = timeFor(tile.state), f = frameOf(t);
    if (f === tile.last) return;
    tile.last = f;
    const items = Statick.scene(tile.state, t);
    tile.native.innerHTML = Statick.svgString(items, { crisp: true });
    tile.zoom.innerHTML = Statick.svgString(items, { w: 243, h: 432 });
    tile.zoom.firstChild.removeAttribute("width"); tile.zoom.firstChild.removeAttribute("height");
  }
  const hero = $("hero-screen");
  let heroLast = -1;
  function drawHero() {
    const t = timeFor("idle"), f = frameOf(t);
    if (f === heroLast) return;
    heroLast = f;
    hero.innerHTML = Statick.svgString(Statick.scene("idle", t));
    hero.firstChild.removeAttribute("width"); hero.firstChild.removeAttribute("height");
  }

  // ---- pixel inspector (the picture rasterised at exactly 135x240, no anti-aliasing, then enlarged without blur) ------------------------------------
  const sel = $("px-state");
  for (const s of Statick.STATES) sel.add(new Option(`${INFO[s][0]} (${s})`, s));
  function rasterise() {
    const t = Number($("px-time").value), items = Statick.scene(sel.value, t);
    $("px-out").textContent = t + " ms";
    const svg = Statick.svgString(items, { crisp: true });
    const img = new Image();
    img.onload = () => {
      for (const id of ["px-native", "px-zoom"]) {
        const ctx = $(id).getContext("2d"); ctx.imageSmoothingEnabled = false; ctx.clearRect(0, 0, 135, 240); ctx.drawImage(img, 0, 0, 135, 240);
      }
    };
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  }
  sel.addEventListener("change", rasterise); $("px-time").addEventListener("input", rasterise);

  // ---- the proposed panel illustration (only what the Stick reports) ---------------------------------------------------------------------------------
  const SCEN = {
    awake: { kind: "awake", dot: "", chip: "דיווח עדכני", line: "ער", sub: "הסטיק דיווח לפני 4 שנ׳ שהמסך ער", age: "לפני 4 שנ׳" },
    sleeping: { kind: "sleeping", dot: "", chip: "דיווח עדכני", line: "ישן: המסך מעומעם", sub: "לפי הדיווח האחרון. הסטיק ממשיך לסנכרן", age: "לפני 6 שנ׳" },
    waiting: { kind: "waiting", dot: "gap", chip: "אין דיווח כרגע", line: "פקודה ממתינה לאישור", sub: "האיור מציג המתנה; הוא לא מראה שהסטיק עסוק", age: "לפני 48 שנ׳" },
    unknown: { kind: "unknown", dot: "never", chip: "טרם דווח", line: "מצב המסך לא ידוע", sub: "האיור באפור ובלי מצמוץ עד שיגיע דיווח", age: "—" },
  };
  let scen = "awake", pmLast = -1;
  function drawPanel() {
    const s = SCEN[scen], t = timeFor("idle"), f = frameOf(t);
    if (f === pmLast) return;
    pmLast = f;
    $("pm-screen").innerHTML = Statick.svgString(Statick.panelScene(s.kind, t), { crisp: false });
    $("pm-screen").firstChild.removeAttribute("width"); $("pm-screen").firstChild.removeAttribute("height");
    $("pm-screen").setAttribute("aria-label", "איור של סטטיק: " + s.line);
  }
  function setScenario(k) {
    scen = k; pmLast = -1; const s = SCEN[k];
    $("pm-dot").className = "dot " + s.dot; $("pm-chip-text").textContent = s.chip;
    $("pm-state").replaceChildren(document.createTextNode(s.line), Object.assign(document.createElement("small"), { textContent: s.sub }));
    $("pm-age").textContent = s.age;
    document.querySelectorAll(".pm-scenarios button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.k === k)));
    drawPanel();
  }
  document.querySelectorAll(".pm-scenarios button").forEach((b) => b.addEventListener("click", () => setScenario(b.dataset.k)));

  // ---- controls ---------------------------------------------------------------------------------------------------------------------------------------
  function syncControls() {
    $("play").setAttribute("aria-pressed", String(playing));
    $("play-label").textContent = playing ? "השהיה" : (reduced() && !userPlayed ? "הפעלת אנימציה" : "המשך");
    const note = $("motion-note");
    note.textContent = freeze !== null ? `הוקפא על ${freeze} ms (בדיקה)` : reduced() && !userPlayed ? "זוהתה בקשה להפחתת תנועה במערכת: האנימציה כבויה, מוצגות תמונות קבועות. אפשר להפעיל ידנית." : "";
  }
  $("play").addEventListener("click", () => {
    if (freeze !== null) return;
    if (playing) { base = clock(); playing = false; }
    else { if (reduced() && !userPlayed) { userPlayed = true; base = 0; } started = performance.now(); playing = true; }
    tiles.forEach((t) => (t.last = -1)); heroLast = -1; pmLast = -1; syncControls();
  });
  $("speed").addEventListener("change", () => { base = clock(); started = performance.now(); speed = Number($("speed").value); });
  reducedQuery.addEventListener("change", () => { playing = !reduced() && freeze === null; tiles.forEach((t) => (t.last = -1)); heroLast = -1; pmLast = -1; syncControls(); });

  function loop() { drawHero(); tiles.forEach(drawTile); drawPanel(); if (playing || freeze === null) requestAnimationFrame(loop); }
  setScenario(q.get("scenario") && SCEN[q.get("scenario")] ? q.get("scenario") : "awake");
  if (q.get("state") && Statick.STATES.includes(q.get("state"))) sel.value = q.get("state");
  if (q.get("pt")) $("px-time").value = q.get("pt");
  rasterise(); syncControls(); loop();
})();
