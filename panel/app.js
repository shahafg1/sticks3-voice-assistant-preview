(() => {
  "use strict";
  // The robot remote. Everything shown comes from GET /api/status (the server's snapshot of the Stick's last report and the stored
  // desired state); every change goes through the same authenticated, CSRF-protected POST endpoints as before. Nothing is invented:
  // "confirmed" is displayed only when the server says the Stick's own report matches, and values the Stick has not reported show "—".
  let csrf = document.querySelector('meta[name="csrf"]').content;
  const $ = (id) => document.getElementById(id);
  const SEND_GAP_MS = 250;       // at most 4 requests a second while a slider moves; the newest value is always sent last
  const RETRY_MS = 3000;
  const FRESH_S = 10, GAP_S = 300;

  let snap = null;               // last status from the server
  let fetchedAt = 0;             // when it arrived (ms), to keep ages moving between polls
  let statusError = false;
  let commandError = "";
  let commandBusy = false;
  let polling = false;

  // ---- API helper shared with reminders.js ----------------------------------------------------------------------
  // The CSRF token belongs to one server process. After a server restart every POST is refused ("bad token"): we fetch the new token
  // (a plain GET, not an action) and report it, but NEVER resend the refused action: the user decides.
  async function refreshToken() {
    const r = await fetch("/", { cache: "no-store" });
    const m = /name="csrf" content="([^"]+)"/.exec(await r.text());
    if (m) csrf = m[1];
  }
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (r.status === 403 && data.error === "bad token") {
      await refreshToken().catch(() => {});
      return { ok: false, status: 403, token: true, data };
    }
    return { ok: r.ok, status: r.status, data };
  }
  window.PanelApi = { post };
  const TOKEN_MSG = "השרת הופעל מחדש, ולכן הפעולה האחרונה לא נשמרה.";

  // ---- text helpers --------------------------------------------------------------------------------------------------
  function ago(seconds) {
    const s = Math.round(seconds);
    if (s < 5) return "ממש עכשיו";
    if (s < 60) return `לפני ${s} שנ׳`;
    const m = Math.floor(s / 60);
    if (m < 60) return m === 1 ? "לפני דקה" : `לפני ${m} דק׳`;
    const h = Math.floor(m / 60);
    return h === 1 ? "לפני שעה" : `לפני ${h} שע׳`;
  }
  const span = (s) => ago(s).replace("לפני ", "");
  function uptime(seconds) {
    if (seconds < 60) return seconds === 1 ? "שנייה אחת" : `${seconds} שניות`;
    const m = Math.floor(seconds / 60);
    if (m < 60) return m === 1 ? "דקה אחת" : `${m} דקות`;
    const h = Math.floor(m / 60);
    if (h < 48) return h === 1 ? "שעה אחת" : `${h} שעות`;
    return `${Math.floor(h / 24)} ימים`;
  }
  const elapsed = () => (Date.now() - fetchedAt) / 1000;
  const reportAge = () => (snap && snap.device.age_s !== null ? snap.device.age_s + elapsed() : null);
  function contactClass() {                      // the server's class; classified locally from the age while the server does not answer
    if (!snap) return null;
    const age = reportAge();
    if (age === null) return "never";
    if (!statusError) return snap.device.contact;
    return age <= FRESH_S ? "fresh" : age <= GAP_S ? "gap" : "stale";
  }
  const SCREEN = { awake: "ער", face: "נכנס לשינה…", dim: "ישן: המסך מעומעם", off: "ישן: המסך כבוי" };
  const TIMEOUT = { 0: "כבויה", 30: "30 שנ׳", 60: "דקה", 300: "5 דק׳" };
  const MODE = { dim: "מעומעם", off: "כבוי" };
  const supports = (need) => !snap || !snap.caps || snap.caps.includes(need);

  // ---- state lines (a dot, a stable text for screen readers, and a ticking counter that is hidden from them) ---------------
  function buildState(el) {
    el.replaceChildren();
    const dot = document.createElement("span"); dot.className = "dot";
    const t = document.createElement("span"); t.className = "t";
    const secs = document.createElement("span"); secs.className = "secs"; secs.setAttribute("aria-hidden", "true");
    el.append(dot, t, secs);
  }
  function setState(el, kind, text, secs, title) {
    if (!el.firstChild) buildState(el);
    el.dataset.kind = kind;
    el.firstChild.className = `dot ${kind}`;
    el.children[1].textContent = text;
    el.children[2].textContent = secs ? ` · ${secs}` : "";
    el.title = title || "";
    for (const extra of [...el.querySelectorAll(".linkbtn")]) extra.remove();
  }

  // ---- controls: faders and sleep preferences ------------------------------------------------------------------------------
  const controls = {};
  function initControl(el) {
    const key = el.dataset.key;
    const slider = el.querySelector('input[type="range"]');
    const c = {
      key, el, slider, radios: [...el.querySelectorAll('input[type="radio"]')],
      out: el.querySelector("output"), state: el.querySelector(".fstate"),
      min: slider ? Number(slider.min) : 0, max: slider ? Number(slider.max) : 0,
      isInt: key !== "sleep_mode", mute: key.endsWith("_volume"),
      local: null, sent: null, sending: false, lastSend: 0, lastInput: 0, timer: null, error: "", retrying: false, dragging: false,
    };
    controls[key] = c;
    buildState(c.state);
    if (slider) {
      slider.addEventListener("pointerdown", () => { c.dragging = true; });
      slider.addEventListener("pointerup", () => { c.dragging = false; });
      slider.addEventListener("pointercancel", () => { c.dragging = false; });
      slider.addEventListener("blur", () => { c.dragging = false; });
      slider.addEventListener("input", () => { c.lastInput = Date.now(); c.local = Number(slider.value); c.error = ""; render(); scheduleSend(c); });
    }
    c.radios.forEach((r) => r.addEventListener("change", () => {
      c.lastInput = Date.now(); c.local = c.isInt ? Number(r.value) : r.value; c.error = ""; render(); scheduleSend(c, 0);
    }));
  }
  const valueText = (c, v) => (c.mute && v === 0 ? "מושתק" : c.key === "idle_timeout_s" ? TIMEOUT[v] : c.key === "sleep_mode" ? MODE[v] : `${v}%`);

  function scheduleSend(c, delay) {
    clearTimeout(c.timer);
    const wait = delay !== undefined ? delay : Math.max(0, SEND_GAP_MS - (Date.now() - c.lastSend));
    c.timer = setTimeout(() => send(c), wait);
  }
  async function send(c) {
    if (c.sending || c.local === null || c.local === c.sent) { if (c.local === c.sent) c.local = null; return; }
    c.sending = true;
    const value = c.local;
    let retryIn = null;
    try {
      const r = await post("/api/setting", { key: c.key, value });
      if (r.ok) {
        adopt(r.data); c.sent = value; c.error = ""; c.retrying = false;
        if (c.local === value) c.local = null;
      } else if (r.status === 429) {
        retryIn = 1000;                                  // the server's own rate limit: wait and send the newest value
      } else if (r.token) {
        c.error = TOKEN_MSG;                             // not resent: the user chooses to try again
      } else {
        c.error = (r.data && r.data.error) || `שגיאה ${r.status}`;
      }
    } catch (e) {
      c.error = "אין תגובה מהשרת";
      retryIn = RETRY_MS;                                // the unfinished change of this one control is retried, nothing else
    } finally {
      c.sending = false; c.lastSend = Date.now(); c.retrying = retryIn !== null;
      if (c.local !== null && c.local !== c.sent && retryIn === null && !c.error) scheduleSend(c);
      else if (c.local !== null && retryIn !== null) scheduleSend(c, retryIn);
      render();
    }
  }

  function renderControl(c) {
    const holder = c.el.closest("[data-caps]");
    const unsupported = holder ? !supports(holder.dataset.caps) : false;
    const input = c.slider;
    const s = snap ? snap.settings[c.key] : null;
    const fallback = c.slider ? (c.key === "brightness" ? 100 : c.max) : (c.key === "sleep_mode" ? "dim" : 0);
    let shown = c.local !== null ? c.local : (s && s.desired !== null ? s.desired : (s && s.reported !== null ? s.reported : fallback));
    if (c.isInt) shown = Number(shown);
    const hasValue = c.local !== null || (s && (s.desired !== null || s.reported !== null));
    if (input) {
      const touching = c.dragging || Date.now() - c.lastInput < 1500;     // polling never moves a slider the user is using
      if (c.local === null && !touching && Number(input.value) !== shown) input.value = shown;
      const v = c.local !== null ? c.local : Number(input.value);
      input.style.setProperty("--pct", `${((v - c.min) / (c.max - c.min)) * 100}%`);
      c.out.textContent = hasValue || c.local !== null || touching ? valueText(c, v) : "—";
      c.out.classList.toggle("muted", c.mute && v === 0);
    } else if (c.local === null && Date.now() - c.lastInput > 1500) {
      c.radios.forEach((r) => { r.checked = String(r.value) === String(shown); });
    }
    c.el.querySelectorAll("input").forEach((i) => { i.disabled = unsupported; });
    const el = c.state;
    if (!snap) { setState(el, "unset", "טוען…"); return; }
    if (unsupported) { setState(el, "unset", "לא נתמך בקושחה הנוכחית"); return; }
    if (c.error) {
      setState(el, "error", c.local !== null ? `לא נשמר: ${c.error}` : c.error, "", "הערך האחרון עוד לא נשמר בשרת");
      if (c.local !== null && !c.retrying) {
        const b = document.createElement("button"); b.type = "button"; b.className = "linkbtn"; b.textContent = "נסו שוב";
        b.addEventListener("click", () => { c.error = ""; scheduleSend(c, 0); render(); });
        el.append(b);
      }
      return;
    }
    if (c.local !== null) { setState(el, "pending", "שומר בשרת…", "", "נשלח לשרת ב-Mac; עוד לא אושר על ידי הסטיק"); return; }
    if (s.state === "unset") {
      setState(el, "unset", "ברירת מחדל", "", s.reported !== null ? `הסטיק משתמש בערך ברירת המחדל שלו (${valueText(c, s.reported)}) עד שתשנו אותו` : "הסטיק משתמש בברירת המחדל שלו");
    } else if (s.state === "pending") {
      const waiting = s.pending_s === null ? 0 : Math.round(s.pending_s + elapsed());
      setState(el, "pending", waiting > 30 ? "עדיין לא אושר" : "ממתין לאישור", `${waiting} שנ׳`,
        `הערך המבוקש (${valueText(c, s.desired)}) נשמר ב-Mac. הסטיק יחיל אותו כשיהיה פנוי ויתחבר לשרת.`);
    } else if (s.state === "confirmed") {
      const stale = contactClass() !== "fresh";
      setState(el, stale ? "stale" : "ok", stale ? "אושר (דיווח ישן)" : "אושר", "",
        `הסטיק דיווח ${ago(reportAge())} שהחיל ${valueText(c, s.reported)}` + (c.mute ? ". ייכנס לתוקף בהשמעה הבאה" : ""));
    } else {
      setState(el, "differs", `הסטיק משתמש בערך אחר (${valueText(c, s.reported)})`, "", `ביקשתם ${valueText(c, s.desired)} והסטיק דיווח ${valueText(c, s.reported)}`);
    }
  }

  // ---- the character: an illustration driven only by reported screen state ---------------------------------------------------
  function renderRobot() {
    const robot = $("robot"), top = $("top");
    const screen = snap ? snap.screen : null;               // from the Stick's last report; null when it has never reported one
    const open = snap && snap.command && (snap.command.state === "queued" || snap.command.state === "sent");
    let expr = "unknown", attr = "unknown";
    if (screen === "awake") { expr = open ? "waiting" : "awake"; attr = "awake"; }   // "waiting": a sleep/wake command is awaiting the Stick
    else if (screen === "face") { expr = "sleeping"; attr = "awake"; }
    else if (screen === "dim") { expr = "sleeping"; attr = "dim"; }
    else if (screen === "off") { expr = "sleeping"; attr = "off"; }
    top.dataset.screen = attr;
    robot.dataset.expr = expr;
    robot.setAttribute("aria-label", `איור של סטטיק: ${{ awake: "ער", waiting: "ממתינה לאישור פקודה", sleeping: "ישן", unknown: "לא ידוע" }[expr]}`);
  }

  // ---- connection: chip, state line, age, battery, banner -----------------------------------------------------------------------
  function renderContact() {
    $("server-line").textContent = statusError ? "אין תגובה מהשרת. מוצג המידע האחרון." : (snap ? "השרת פועל." : "טוען…");
    $("server-line").classList.toggle("bad", statusError);
    const chipDot = $("contact-dot"), chipText = $("contact-text"), banner = $("banner");
    if (!snap) { chipDot.className = "dot unset"; chipText.textContent = statusError ? "השרת אינו מגיב" : "טוען…"; banner.hidden = true; return; }
    const contact = contactClass();
    const age = reportAge();
    const r = snap.device.report;
    const chip = { never: ["unset", "טרם דיווח"], fresh: ["ok", "דיווח עדכני"], gap: ["pending", "אין דיווח כרגע"], stale: ["stale", "מידע לא עדכני"] }[contact];
    chipDot.className = `dot ${chip[0]}`; chipText.textContent = chip[1];
    $("age").textContent = age === null ? "אין עדיין" : ago(age);
    const battery = r && r.battery !== undefined ? r.battery : null;
    $("batt").textContent = battery === null ? "—" : `${battery}%`;
    $("batt-fill").setAttribute("width", battery === null ? 0 : Math.max(1, Math.round(12 * battery / 100)));
    $("batt-bolt").hidden = !(r && r.charging);

    $("state-text").textContent = snap.screen ? (SCREEN[snap.screen] || snap.screen) : "מצב המסך לא ידוע";
    $("state-sub").textContent = !snap.screen ? "אין עדיין דיווח על מצב המסך" : contact === "fresh" ? "לפי הדיווח האחרון" : `לפי דיווח מלפני ${span(age)}`;

    let kind = "", text = "";
    if (statusError) { kind = "error"; text = "אין תגובה מהשרת. מוצג המידע האחרון; שינויים לא יישמרו עד שהשרת יחזור."; }
    else if (contact === "gap") {
      kind = "gap";
      text = `אין דיווח כרגע (האחרון ${ago(age)}). הסטיק מדווח רק כשהוא פנוי ועומד במקום, ולכן ייתכן שהוא עסוק או שאינו זמין.` +
        (snap.device.voice_job_active ? " כרגע יש שאלה בעיבוד בשרת." : "") + " שינויים נשמרים ב-Mac ויוחלו כשידווח.";
    } else if (contact === "stale") {
      kind = "stale";
      text = `לא התקבל דיווח מזה ${span(age)}. ייתכן שהסטיק כבוי או רחוק מהרשת, והערכים שמוצגים ישנים. שינויים יישמרו ויוחלו כשיתחבר; פקודות שינה והערה פוקעות.`;
    } else if (contact === "never") {
      kind = "stale";
      text = snap.key_provisioned ? "הסטיק עוד לא דיווח מאז שהשרת עלה. עד אז לא יוצג מצב מאושר." : "מפתח המכשיר עדיין לא הוגדר בשרת (ראו docs/desktop-panel.md).";
    }
    banner.hidden = !kind; banner.className = `banner ${kind}`; banner.textContent = text;
    $("top").classList.toggle("stale-values", contact === "gap" || contact === "stale");
  }

  // ---- sleep / wake keys ----------------------------------------------------------------------------------------------------------
  function renderCommand() {
    const line = $("cmd-line");
    if (!line.firstChild) { line.append(document.createElement("span"), document.createElement("span")); line.children[1].setAttribute("aria-hidden", "true"); }
    const [t, secs] = line.children;
    if (!snap) { t.textContent = ""; secs.textContent = ""; return; }
    if (commandError) { t.textContent = commandError; secs.textContent = ""; return; }
    const c = snap.command;
    const recent = c && (c.ttl_left_s !== null || c.age_s + elapsed() < 180);
    if (!recent) { t.textContent = ""; secs.textContent = ""; return; }
    const what = c.type === "sleep" ? "שינה" : "הערה";
    const view = {
      queued: [`פקודת ${what} ממתינה לסטיק`, true],
      sent: [`פקודת ${what} נשלחה לסטיק וממתינה לביצוע (עוד לא בוצעה)`, true],
      executed: [`הסטיק דיווח שביצע: ${what}`, false],
      expired: [`פקודת ${what} פגה לפני שהסטיק ביצע אותה, ולא תבוצע מאוחר יותר. אפשר ללחוץ שוב`, false],
      failed: [`פקודת ${what} לא בוצעה: הסטיק הופעל מחדש, והיא לא תישלח שוב`, false],
      ignored: [`הסטיק התעלם מפקודת ${what}`, false],
      superseded: [`פקודת ${what} הוחלפה בפקודה חדשה`, false],
    }[c.state];
    t.textContent = view[0];
    const left = c.ttl_left_s === null ? null : Math.max(0, Math.round(c.ttl_left_s - elapsed()));
    secs.textContent = view[1] && left !== null ? ` · תפוג בעוד ${left} שנ׳` : "";
  }
  async function sendCommand(type) {
    if (commandBusy) return;
    commandBusy = true; commandError = "";
    try {
      const r = await post("/api/command", { type });
      if (r.ok) adopt(r.data);
      else if (r.token) commandError = `${TOKEN_MSG} אפשר ללחוץ שוב.`;
      else if (r.status === 429) commandError = "יותר מדי לחיצות. נסו שוב עוד רגע.";
      else commandError = (r.data && r.data.error) || `שגיאה ${r.status}`;
    } catch (e) {
      commandError = "אין תגובה מהשרת. הפקודה לא נשלחה";
    }
    commandBusy = false;
    render();
  }

  // ---- capabilities and technical details ---------------------------------------------------------------------------------------------
  function renderCaps() {
    document.querySelectorAll("[data-caps]").forEach((el) => {
      if (el.classList.contains("control")) return;               // controls handle themselves
      const ok = supports(el.dataset.caps);
      el.querySelectorAll("input, button").forEach((i) => { i.disabled = !ok; });
    });
    const missing = [["volume", "עוצמות קול"], ["sleep", "שינה"]].filter(([need]) => !supports(need)).map(([, name]) => name);
    const note = $("caps-note");
    note.hidden = missing.length === 0;
    note.textContent = missing.length ? `הקושחה בסטיק ישנה ואינה תומכת ב: ${missing.join(", ")}. יש לעדכן את הסטיק.` : "";
  }
  let techSig = "";
  function renderTech() {
    const r = snap && snap.device.report;
    const rows = [];
    if (r) {
      if (r.app) rows.push(["גרסת האפליקציה", r.app]);
      if (r.fw) rows.push(["קושחה", r.fw]);
      if (r.brightness !== undefined) rows.push(["בהירות מדווחת", `${r.brightness}%`]);
      if (r.rssi !== undefined) rows.push(["עוצמת Wi-Fi", `${r.rssi} dBm`]);
      if (r.uptime !== undefined) rows.push(["זמן פעולה", uptime(r.uptime), true]);
      rows.push(["מזהה מצב", `${snap.brightness.store} · rev ${r.rev}`]);
      if (r.boot) rows.push(["מזהה הפעלה", r.boot]);
      if (r.caps) rows.push(["יכולות", r.caps.join(", ")]);
    }
    const sig = JSON.stringify(rows);
    if (sig === techSig) return;
    techSig = sig;
    const list = $("tech-list");
    list.replaceChildren();
    for (const [name, value, plain] of rows) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = name;
      if (plain) dd.textContent = value; else { const b = document.createElement("bdi"); b.dir = "ltr"; b.textContent = value; dd.append(b); }
      list.append(dt, dd);
    }
    if (!rows.length) { const p = document.createElement("dd"); p.textContent = "אין עדיין דיווח."; list.append(p); }
  }

  function render() {
    renderContact();
    Object.values(controls).forEach(renderControl);
    renderRobot();
    renderCommand();
    renderCaps();
    renderTech();
    const t = controls.idle_timeout_s, m = controls.sleep_mode;
    const sv = (c) => { const s = snap && snap.settings[c.key]; return c.local !== null ? c.local : s && s.desired !== null ? s.desired : s && s.reported !== null ? s.reported : null; };
    const a = sv(t), b = sv(m);
    $("sleep-summary").textContent = a === null && b === null ? "" : `${a === null ? "" : a === 0 ? "ללא שינה אוטומטית" : "אחרי " + TIMEOUT[a]}${a !== null && b !== null ? " · " : ""}${b === null ? "" : MODE[b]}`;
  }

  function adopt(data) {                                          // a snapshot from GET /api/status or from the response of a POST
    snap = data; fetchedAt = Date.now(); statusError = false;
    document.dispatchEvent(new CustomEvent("snapshot", { detail: snap }));
  }
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      adopt(await r.json());
    } catch (e) {
      statusError = true;
    }
    polling = false;
    render();
  }

  // ---- tabs (ARIA tab pattern; arrows, Home and End) ------------------------------------------------------------------------------------
  const tabs = [$("tab-remote"), $("tab-rem")];
  function selectTab(i, focus) {
    tabs.forEach((t, j) => { t.setAttribute("aria-selected", String(i === j)); t.tabIndex = i === j ? 0 : -1; });
    $("panel-remote").hidden = i !== 0; $("panel-rem").hidden = i !== 1;
    if (focus) tabs[i].focus();
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => selectTab(i));
    t.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); selectTab(1 - i, true); }
      else if (e.key === "Home") { e.preventDefault(); selectTab(0, true); }
      else if (e.key === "End") { e.preventDefault(); selectTab(1, true); }
    });
  });

  document.querySelectorAll(".control").forEach(initControl);
  $("btn-sleep").addEventListener("click", () => sendCommand("sleep"));
  $("btn-wake").addEventListener("click", () => sendCommand("wake"));
  poll();
  setInterval(poll, 1000);
  setInterval(render, 1000);
})();
