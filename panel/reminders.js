(() => {
  "use strict";
  // The reminders tab. It talks to the existing reminder endpoints through PanelApi (CSRF, rate limits and refusals unchanged).
  // Rules: background polling only redraws the LISTS (when something really changed); it never touches the form. An unfinished form is
  // never replaced, a refused or failed save keeps everything typed, and a retried save reuses one client key, so it cannot create
  // a duplicate even if the first attempt did reach the server. All server text is inserted with textContent.
  const $ = (id) => document.getElementById(id);
  const form = $("rem-form");
  const TZ = "Asia/Jerusalem";

  let snap = null;
  let editing = null;            // {id, occ} while editing a pending reminder
  let fold = null;               // 0 | 1 once the user picked one of two identical local times
  let clientKey = crypto.randomUUID();
  let busy = false;
  let previewTimer = null;
  let previewSeq = 0;
  let confirming = null;         // id of the reminder whose cancel button is armed
  let confirmTimer = null;
  let drawn = "";

  const ERRORS = {
    past: "הזמן שבחרתם כבר עבר. בחרו זמן עתידי.",
    too_far: "הזמן רחוק מדי (יותר משנה).",
    nonexistent: "השעה הזו לא קיימת (השעון קופץ קדימה במעבר לשעון קיץ). בחרו שעה אחרת.",
    invalid: "יש למלא טקסט ולבחור זמן תקין.",
    too_many: "יש יותר מדי תזכורות ממתינות (עד 50).",
    no_speech: "הקראה בקול אינה זמינה בשרת הזה. בחרו צלצול.",
    conflict: "התזכורת כבר נשלחה לסטיק, בוטלה או שונתה, ולכן אי אפשר לשנות אותה עכשיו.",
    not_found: "התזכורת לא נמצאה.",
  };
  const NETWORK = "אין תגובה מהשרת. מה שהקלדתם נשמר בטופס. אפשר לנסות שוב: לחיצה חוזרת לא תיצור תזכורת כפולה.";
  const RESTARTED = "השרת הופעל מחדש, ולכן הפעולה לא נשמרה. מה שהקלדתם נשמר בטופס; אפשר ללחוץ שוב (לא ייווצרו כפילויות).";
  const MODE = { speak: "הקראה בקול", chime: "צלצול והודעה" };
  const dateFmt = new Intl.DateTimeFormat("he-IL", { timeZone: TZ, dateStyle: "full", timeStyle: "short" });
  const whenText = (item) => `${dateFmt.format(new Date(item.due_at * 1000))} (שעון ישראל, UTC${item.offset})`;
  const minutes = (s) => { const m = Math.max(1, Math.round(s / 60)); return m === 1 ? "דקה" : `${m} דקות`; };
  const supported = () => !snap || !snap.caps || snap.caps.includes("reminder");

  function statusView(item) {
    const parts = [];
    let kind = "neutral", text = "";
    switch (item.status) {
      case "scheduled":
        if (item.reason === "retry_after_restart") { kind = "warn"; text = "ממתינה למסירה חוזרת (הסטיק הופעל מחדש)"; }
        else if (item.overdue_s > 0) { kind = "warn"; text = "הגיע זמנה: ממתינה לסטיק פנוי"; }
        else { kind = "info"; text = "מתוזמנת"; }
        if (item.mode === "speak" && item.audio === "pending") parts.push("מכינה את הקול…");
        if (item.mode === "speak" && item.audio === "failed") parts.push("הכנת הקול נכשלה: תימסר כצלצול בלבד");
        break;
      case "sent": kind = "info"; text = "נשלחה לסטיק, ממתינה לאישור"; break;
      case "received": kind = "info"; text = "הסטיק קיבל"; break;
      case "presented": kind = "info"; text = "מוצגת בסטיק עכשיו"; break;
      case "completed":
        kind = "ok";
        text = item.reason === "muted" ? "הוצגה על המסך (הקול מושתק)" : (item.delivered_mode === "speak" ? "הושמעה עד הסוף" : "הוצגה עם צלצול");
        break;
      case "dismissed": kind = "ok"; text = "נעצרה בלחיצה על הכפתור"; break;
      case "missed":
        kind = "bad";
        text = item.reason === "late_on_device" ? "הוחמצה: הגיעה לסטיק מאוחר מדי" : `הוחמצה: לא נמסרה תוך ${snap.reminders.grace_min} דקות מהזמן שלה`;
        break;
      case "failed":
        kind = "bad";
        text = {
          restart: "נכשלה: הסטיק הופעל מחדש במהלך המסירה",
          interrupted: "נכשלה: הסטיק הופעל מחדש בזמן שהוצגה",
          media: "נכשלה: הסטיק לא הצליח להוריד את הקול",
          device: "נכשלה בסטיק",
          unconfirmed: "לא התקבל אישור מהסטיק",
        }[item.reason] || "נכשלה";
        break;
      case "cancelled": kind = "neutral"; text = "בוטלה"; break;
      default: text = item.status;
    }
    if (item.late) parts.push(`נמסרה באיחור של ${minutes(item.late_s)}`);
    if (item.reason === "voice_unavailable") parts.push("הקול לא היה זמין: נמסרה בצלצול");
    if (item.reason === "retry_after_restart" && item.status !== "scheduled") parts.push("נשלחה שוב אחרי הפעלה מחדש של הסטיק");
    return { kind, text, extra: parts.join(" · ") };
  }

  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function itemNode(item, pending) {
    const li = el("li", "rem-item");
    li.dataset.id = item.id;
    const text = el("div", "rem-text", item.text);
    text.dir = "auto";
    const when = el("div", "rem-when", whenText(item));
    const view = statusView(item);
    const status = el("div", "rem-status");
    status.append(el("span", `badge ${view.kind}`, view.text), el("span", "mode-tag", MODE[item.mode]));
    if (view.extra) status.append(el("span", "fineprint", view.extra));
    li.append(text, when, status);
    if (pending && item.status === "scheduled") {                 // only a still-scheduled reminder can change; a sent one cannot be recalled
      const actions = el("div", "rem-actions");
      const edit = el("button", "key small", "עריכה");
      edit.type = "button"; edit.dataset.act = "edit"; edit.dataset.id = item.id;
      edit.addEventListener("click", () => startEdit(item));
      const cancel = el("button", "key small", confirming === item.id ? "לאשר ביטול?" : "ביטול");
      cancel.type = "button"; cancel.dataset.act = "cancel"; cancel.dataset.id = item.id;
      cancel.addEventListener("click", () => cancelReminder(item));
      actions.append(edit, cancel);
      li.append(actions);
    }
    return li;
  }

  const signature = (r) => JSON.stringify([r.pending.map((i) => ({ ...i, overdue_s: i.overdue_s > 0 })), r.history, confirming]);

  function renderLists() {
    if (!snap || !snap.reminders) return;
    const r = snap.reminders;
    $("rem-count").hidden = r.pending.length === 0;
    $("rem-count").textContent = r.pending.length;
    const sig = signature(r);
    if (sig !== drawn) {                                          // redraw only on a real change, so a click or focus is never lost
      drawn = sig;
      const active = document.activeElement;
      const keep = active && active.dataset && active.dataset.act && $("rem-pending").contains(active) ? [active.dataset.act, active.dataset.id] : null;
      $("rem-grace").textContent = r.grace_min;
      $("rem-pending").replaceChildren(...r.pending.map((i) => itemNode(i, true)));
      $("rem-history").replaceChildren(...r.history.map((i) => itemNode(i, false)));
      $("rem-pending-empty").hidden = r.pending.length > 0;
      $("rem-history-empty").hidden = r.history.length > 0;
      $("hist-count").textContent = r.history.length ? `(${r.history.length})` : "";
      if (keep) {
        const again = $("rem-pending").querySelector(`button[data-act="${keep[0]}"][data-id="${keep[1]}"]`);
        if (again) again.focus();
      }
    }
    const ok = supported();
    $("rem-unsupported").hidden = ok;
    $("rem-mode-speak").disabled = !r.speech_available;
    if (!r.speech_available && $("rem-mode-speak").checked) $("rem-mode-chime").checked = true;
    $("rem-save").disabled = busy || !ok;
    renderEditWarning();
  }

  // an edit in progress is never discarded; if its reminder changed meanwhile we only say so
  function renderEditWarning() {
    const box = $("rem-edit-warning");
    if (!editing || !snap) { box.hidden = true; return; }
    const item = snap.reminders.pending.find((i) => i.id === editing.id);
    let text = "";
    if (!item) text = "התזכורת הזו כבר לא ממתינה (נשלחה, בוטלה או הוחמצה). אפשר לשמור את הנוסח כתזכורת חדשה.";
    else if (item.occ !== editing.occ || item.status !== "scheduled") text = "התזכורת השתנתה או נשלחה לסטיק מאז שהתחלתם לערוך, ולכן שמירה כעריכה תיכשל.";
    box.hidden = !text; box.textContent = text;
  }

  // ---- the form ----------------------------------------------------------------------------------------------------------------------
  const mode = () => form.querySelector('input[name="rmode"]:checked').value;
  const whenKind = () => form.querySelector('input[name="when"]:checked').value;
  function whenValue() {
    if (whenKind() === "in") return { kind: "in", minutes: Number($("rem-minutes").value) };
    return { kind: "at", local: $("rem-local").value };
  }
  function showError(message) { const p = $("rem-error"); p.hidden = !message; p.textContent = message || ""; }
  const api = (path, body) => window.PanelApi.post(path, body);

  function showFoldChoice(options) {
    $("rem-fold").hidden = false;
    options.forEach((o, i) => { $(`rem-fold-${i}`).textContent = `${i === 0 ? "הפעם הראשונה" : "הפעם השנייה"} (UTC${o.offset})`; });
  }
  function clearFold() { fold = null; $("rem-fold").hidden = true; }

  async function preview() {
    const seq = ++previewSeq;
    const box = $("rem-preview");
    const w = whenValue();
    if ((w.kind === "in" && !(w.minutes >= 1)) || (w.kind === "at" && !w.local)) { box.textContent = ""; box.className = "preview"; return; }
    let res;
    try { res = await api("/api/reminders/preview", { when: w, fold }); } catch (e) { return; }   // a lost preview changes nothing
    if (seq !== previewSeq || res.status === 429) return;
    if (res.token) { box.className = "preview bad"; box.textContent = "השרת הופעל מחדש. מעדכן את התצוגה…"; schedulePreview(); return; }
    if (res.ok) {
      $("rem-fold").hidden = true;
      box.className = "preview";
      box.replaceChildren("מועד המסירה: ", el("b", null, whenText(res.data.preview)), document.createElement("br"),
        mode() === "speak" ? "הקראה בקול בעברית" : "צלצול והודעה כללית בסטיק");
    } else if (res.data.code === "ambiguous") {
      box.className = "preview bad"; box.textContent = "יש לבחור איזו משתי השעות."; showFoldChoice(res.data.options);
    } else {
      box.className = "preview bad"; box.textContent = ERRORS[res.data.code] || "הזמן אינו תקין."; $("rem-fold").hidden = true;
    }
  }
  function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(preview, 300); }

  function resetForm() {
    editing = null; clearFold();
    $("rem-text").value = "";
    form.querySelector('input[name="when"][value="in"]').checked = true;    // back to the defaults: in 10 minutes, spoken
    $("rem-minutes").value = 10; $("rem-local").value = "";
    (!$("rem-mode-speak").disabled ? $("rem-mode-speak") : $("rem-mode-chime")).checked = true;
    syncWhen(false);
    $("rem-save").textContent = "שמירת תזכורת"; $("rem-form-title").textContent = "תזכורת חדשה";
    $("rem-cancel-edit").hidden = true;
    clientKey = crypto.randomUUID();
    showError(""); $("rem-edit-warning").hidden = true;
    schedulePreview();
  }
  function startEdit(item) {
    editing = { id: item.id, occ: item.occ };
    clearFold();
    $("rem-text").value = item.text;
    form.querySelector(`input[name="rmode"][value="${item.mode}"]`).checked = true;
    form.querySelector('input[name="when"][value="at"]').checked = true; syncWhen(false);
    $("rem-local").value = item.local.replace(" ", "T");
    $("rem-save").textContent = "עדכון תזכורת"; $("rem-form-title").textContent = "עריכת תזכורת"; $("rem-cancel-edit").hidden = false;
    showError(""); renderEditWarning(); schedulePreview(); $("rem-text").focus();
  }
  function syncWhen(preview_ = true) {
    const at = whenKind() === "at";
    $("when-in").hidden = at; $("when-at").hidden = !at;
    if (preview_) { clearFold(); schedulePreview(); }
  }

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    busy = true; renderLists(); showError("");
    try {
      const body = { text: $("rem-text").value, mode: mode(), when: whenValue(), fold };
      const res = editing
        ? await api("/api/reminders/edit", { ...body, id: editing.id, occ: editing.occ })
        : await api("/api/reminders", { ...body, client_key: clientKey });
      if (res.ok) {
        document.dispatchEvent(new CustomEvent("snapshot", { detail: res.data }));
        resetForm();
      } else if (res.token) {
        showError(RESTARTED);
      } else if (res.data.code === "ambiguous") {
        showFoldChoice(res.data.options); showError("יש לבחור איזו משתי השעות.");
      } else if (res.status === 429) {
        showError("יותר מדי בקשות. נסו שוב עוד רגע.");
      } else if (res.data.code === "conflict" && editing) {
        editing = null;                                            // keep what was typed: it can still be saved as a new reminder
        $("rem-save").textContent = "שמירת תזכורת"; $("rem-form-title").textContent = "תזכורת חדשה"; $("rem-cancel-edit").hidden = true;
        showError(`${ERRORS.conflict} הנוסח נשמר בטופס: אפשר לשמור אותו כתזכורת חדשה.`);
      } else {
        showError(ERRORS[res.data.code] || res.data.error || "השמירה נכשלה.");
      }
    } catch (e) {
      showError(NETWORK);
    } finally {
      busy = false; renderLists();
    }
  }

  async function cancelReminder(item) {
    if (confirming !== item.id) {                                  // two clicks, no browser dialog
      confirming = item.id; renderLists();
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(() => { confirming = null; renderLists(); }, 4000);
      return;
    }
    confirming = null;
    try {
      const res = await api("/api/reminders/cancel", { id: item.id, occ: item.occ });
      if (res.ok) {
        document.dispatchEvent(new CustomEvent("snapshot", { detail: res.data }));
        if (editing && editing.id === item.id) resetForm();
      } else if (res.token) showError(RESTARTED);
      else showError(ERRORS[res.data.code] || res.data.error || "הביטול נכשל.");
    } catch (e) {
      showError("אין תגובה מהשרת. התזכורת לא בוטלה.");
    }
    renderLists();
  }

  form.addEventListener("submit", submit);
  $("rem-cancel-edit").addEventListener("click", resetForm);
  [$("rem-minutes"), $("rem-local")].forEach((i) => i.addEventListener("input", () => { clearFold(); schedulePreview(); }));
  form.querySelectorAll('input[name="when"]').forEach((i) => i.addEventListener("change", () => syncWhen()));
  form.querySelectorAll('input[name="rmode"]').forEach((i) => i.addEventListener("change", schedulePreview));
  $("rem-text").addEventListener("input", () => showError(""));
  $("min-minus").addEventListener("click", () => { $("rem-minutes").value = Math.max(1, Number($("rem-minutes").value || 1) - 5); clearFold(); schedulePreview(); });
  $("min-plus").addEventListener("click", () => { $("rem-minutes").value = Math.min(10080, Number($("rem-minutes").value || 0) + 5); clearFold(); schedulePreview(); });
  [0, 1].forEach((i) => $(`rem-fold-${i}`).addEventListener("click", () => { fold = i; preview(); }));
  document.addEventListener("snapshot", (event) => { snap = event.detail; renderLists(); });
  // The preview is a request to the server: ask for it only once the person opens this tab (never on page load).
  $("tab-rem").addEventListener("click", schedulePreview);
})();
