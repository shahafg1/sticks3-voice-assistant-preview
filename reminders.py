"""One-time manual reminders: scheduling, delivery state machine, and local-TTS audio assets (docs/reminders.md).

The Mac stores reminders in SQLite (the same database as the panel settings) and hands at most one at a time to
the Stick inside the authenticated /device/sync reply; the Stick only syncs while it is idle, so a reminder never
interrupts recording, thinking, downloading, playback, a shake reaction or another notification.

Statuses: scheduled -> sent -> received -> presented -> completed | dismissed | failed, plus missed (never delivered
within the grace window) and cancelled. "sent" means the server put it in a sync reply: that is NOT proof the Stick
got it. "received" and "presented" come from the Stick itself (a short authenticated state report, or the audio
download); "completed" means the Stick says the speech or chime played to the end. Nothing here claims exactly-once
audible delivery: see "Crash windows" in docs/reminders.md.

All SQL that changes a reminder is one short transaction guarded by the status and occurrence id it expects, so a
delivery, an edit and a cancel racing each other have exactly one winner.
"""
import re
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_NAME = "Asia/Jerusalem"
TZ = ZoneInfo(TZ_NAME)
GRACE_S = 30 * 60              # a reminder may be delivered this much after its time; later it is marked missed
LATE_AFTER_S = 60              # delivered later than this after its time: labelled late in the panel
MIN_LEAD_S = 5                 # a reminder must be due at least this far in the future
MAX_LEAD_S = 366 * 86400
MAX_MINUTES = 7 * 24 * 60
MAX_TEXT = 200
MAX_PENDING = 50
HISTORY_KEEP = 100             # finished reminders kept in the database
MIN_GAP_S = 15                 # quiet time after one delivery finished before the next may start (no bursts)
RESEND_AFTER_S = 4             # an unacknowledged "sent" reminder is offered again after this long (lost reply)
UNCONFIRMED_S = 600            # a delivered reminder with no word from the Stick for this long is marked failed
AUDIO_WAIT_S = 60              # a speech reminder waits this long past its time for its audio, then falls back to a chime
AUDIO_TRIES = 3
MAX_AUDIO_BYTES = 800_000      # about 25 s of 16 kHz mono 16-bit speech
MAX_ATTEMPTS = 2               # at most one redelivery after a Stick restart during delivery
LABEL_MAX = 24

MODES = ("speak", "chime")
OPEN = ("sent", "received", "presented")
TERMINAL = ("completed", "dismissed", "missed", "failed", "cancelled")
# what the Stick may tell us. muted = shown but silent (volume 0); expired = it arrived too late to start.
DEVICE_RESULTS = ("completed", "dismissed", "muted", "failed", "media_failed", "expired", "interrupted")
DEVICE_STAGES = ("received", "presented")
COLUMNS = ("id", "occ", "client_key", "text", "mode", "due_at", "created_at", "updated_at", "status", "reason",
           "attempts", "audio", "audio_tries", "delivered_mode", "sent_at", "sent_boot", "received_at",
           "presented_at", "finished_at")
SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM reminders"


class ReminderError(ValueError):
    """A request the user can fix. code is stable for the panel; options is set for an ambiguous local time."""
    def __init__(self, code, message, options=None, status=400):
        super().__init__(message)
        self.code, self.options, self.status = code, options, status


# ---- time resolution (pure) ----------------------------------------------------------------------------

def _offset_text(moment):
    minutes = int(moment.utcoffset().total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return "%s%02d:%02d" % (sign, abs(minutes) // 60, abs(minutes) % 60)


def describe(due_at):
    """Resolved view of an epoch time in Asia/Jerusalem."""
    moment = datetime.fromtimestamp(due_at, TZ)
    return {"due_at": due_at, "local": moment.strftime("%Y-%m-%d %H:%M"), "offset": _offset_text(moment), "tz": TZ_NAME}


def resolve_when(when, now, fold=None):
    """Turn {"kind": "in", "minutes": N} or {"kind": "at", "local": "YYYY-MM-DDTHH:MM", "fold": 0|1} into an epoch time.
    Never silently picks a different time: a past time, a nonexistent local time (spring-forward gap) and an
    ambiguous one (the repeated hour in autumn) are all errors, the ambiguous one offering both candidates."""
    if not isinstance(when, dict):
        raise ReminderError("invalid", "when is required")
    kind = when.get("kind")
    if kind == "in":
        minutes = when.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_MINUTES:
            raise ReminderError("invalid", f"minutes must be a whole number from 1 to {MAX_MINUTES}")
        due = int(now) + minutes * 60
    elif kind == "at":
        text = when.get("local")
        match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", text) if isinstance(text, str) else None
        if not match:
            raise ReminderError("invalid", "local time must look like 2026-11-19T09:30")
        try:
            naive = datetime(*map(int, match.groups()))
        except ValueError:
            raise ReminderError("invalid", "not a real date or time") from None
        first, second = naive.replace(tzinfo=TZ, fold=0), naive.replace(tzinfo=TZ, fold=1)
        if first.utcoffset() != second.utcoffset():
            back = first.astimezone(timezone.utc).astimezone(TZ).replace(tzinfo=None)
            if back != naive:
                raise ReminderError("nonexistent", "that local time does not exist (the clock jumps forward); choose another time")
            if fold not in (0, 1) or isinstance(fold, bool):
                options = [{"fold": 0, "due_at": int(first.timestamp()), "offset": _offset_text(first)},
                           {"fold": 1, "due_at": int(second.timestamp()), "offset": _offset_text(second)}]
                raise ReminderError("ambiguous", "that local time happens twice (the clock goes back); choose which one", options)
            chosen = second if fold == 1 else first
        else:
            chosen = first
        due = int(chosen.timestamp())
    else:
        raise ReminderError("invalid", "unknown kind of time")
    if due < now + MIN_LEAD_S:
        raise ReminderError("past", "the time has already passed")
    if due > now + MAX_LEAD_S:
        raise ReminderError("too_far", "the time is more than a year away")
    return due


def clean_text(value):
    if not isinstance(value, str):
        raise ReminderError("invalid", "text is required")
    text = re.sub(r"[\x00-\x1f\x7f‎‏‪-‮⁦-⁩]+", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ReminderError("invalid", "text is required")
    if len(text) > MAX_TEXT:
        raise ReminderError("invalid", f"text is longer than {MAX_TEXT} characters")
    return text


def ascii_label(text):
    """What the Stick may show: its screen font has no Hebrew, so only the printable ASCII of the text (or a generic word)."""
    label = re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e]", " ", text)).strip()[:LABEL_MAX].strip()
    return label if re.search(r"[A-Za-z0-9]", label) else "Reminder"


def audio_name(reminder_id, occ):
    return f"r{reminder_id}_o{occ}.wav"


class ReminderService:
    def __init__(self, store, audio_dir, clock=time.time, synth=None, busy=lambda: False, sleep=time.sleep):
        """synth(text, out_path) must write a 16 kHz mono 16-bit WAV to out_path or raise; None = speech unavailable."""
        self.store = store
        self.audio_dir = Path(audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._clock, self._synth, self._busy, self._sleep = clock, synth, busy, sleep
        self._jobs = []                         # [not_before, occ] waiting for the single audio worker
        self._jobs_lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self.audio_log = []                     # (occ, outcome) for tests and the server log
        self.sweep_orphans()
        self._requeue_audio()

    # ---- rows --------------------------------------------------------------------------------------
    @staticmethod
    def _row(row):
        return dict(zip(COLUMNS, row)) if row else None

    def _get(self, con, where, args=()):
        return self._row(con.execute(f"{SELECT} WHERE {where}", args).fetchone())

    def get(self, reminder_id):
        with closing(self.store.connect()) as con:
            return self._get(con, "id = ?", (reminder_id,))

    def get_by_occ(self, occ):
        with closing(self.store.connect()) as con:
            return self._get(con, "occ = ?", (occ,))

    @staticmethod
    def _next_occ(con, now):
        last = con.execute("SELECT COALESCE(MAX(occ), 0) FROM reminders").fetchone()[0]
        return max(last + 1, int(now * 1000))

    # ---- panel side: create / edit / cancel ----------------------------------------------------------
    def _validated(self, text, mode, when, fold, now):
        text = clean_text(text)
        if mode not in MODES:
            raise ReminderError("invalid", "mode must be speak or chime")
        if mode == "speak" and self._synth is None:
            raise ReminderError("no_speech", "spoken reminders are not available on this server")
        return text, mode, resolve_when(when, now, fold)

    def create(self, text, mode, when, fold=None, client_key=None):
        """Store a new reminder; a repeated client_key returns the earlier one (a retried request never duplicates)."""
        now = self._clock()
        text, mode, due = self._validated(text, mode, when, fold, now)
        client_key = client_key[:64] if isinstance(client_key, str) and client_key else None
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            if client_key:
                again = self._get(con, "client_key = ?", (client_key,))
                if again:
                    con.execute("COMMIT")
                    return again
            pending = con.execute("SELECT COUNT(*) FROM reminders WHERE status = 'scheduled'").fetchone()[0]
            if pending >= MAX_PENDING:
                con.execute("ROLLBACK")
                raise ReminderError("too_many", f"at most {MAX_PENDING} pending reminders")
            occ = self._next_occ(con, now)
            audio = "pending" if mode == "speak" else "none"
            cursor = con.execute(
                "INSERT INTO reminders (occ, client_key, text, mode, due_at, created_at, updated_at, status, audio) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', ?)", (occ, client_key, text, mode, due, now, now, audio))
            row = self._get(con, "id = ?", (cursor.lastrowid,))
            con.execute("COMMIT")
        if mode == "speak":
            self.enqueue(occ)
        return row

    def edit(self, reminder_id, expected_occ, text, mode, when, fold=None):
        """Change a still-scheduled reminder. It gets a NEW occurrence id, so audio, queued work and acknowledgements
        of the old version are invalidated. 409 if it was already sent, changed or cancelled meanwhile."""
        now = self._clock()
        text, mode, due = self._validated(text, mode, when, fold, now)
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            old = self._get(con, "id = ?", (reminder_id,))
            if old is None:
                con.execute("ROLLBACK")
                raise ReminderError("not_found", "no such reminder", status=404)
            if old["status"] != "scheduled" or old["occ"] != expected_occ:
                con.execute("ROLLBACK")
                raise ReminderError("conflict", self._conflict_message(old), status=409)
            occ = self._next_occ(con, now)
            con.execute("UPDATE reminders SET occ = ?, text = ?, mode = ?, due_at = ?, updated_at = ?, "
                        "audio = ?, audio_tries = 0 WHERE id = ?",
                        (occ, text, mode, due, now, "pending" if mode == "speak" else "none", reminder_id))
            row = self._get(con, "id = ?", (reminder_id,))
            con.execute("COMMIT")
        self._remove_audio(old)
        if mode == "speak":
            self.enqueue(occ)
        return row

    def cancel(self, reminder_id, expected_occ):
        now = self._clock()
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            old = self._get(con, "id = ?", (reminder_id,))
            if old is None:
                con.execute("ROLLBACK")
                raise ReminderError("not_found", "no such reminder", status=404)
            if old["status"] != "scheduled" or old["occ"] != expected_occ:
                con.execute("ROLLBACK")
                raise ReminderError("conflict", self._conflict_message(old), status=409)
            con.execute("UPDATE reminders SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, reminder_id))
            row = self._get(con, "id = ?", (reminder_id,))
            con.execute("COMMIT")
        self._remove_audio(old)
        return row

    @staticmethod
    def _conflict_message(row):
        if row["status"] in OPEN:
            return "already sent to the Stick; it can no longer be changed or cancelled"
        if row["status"] != "scheduled":
            return f"already {row['status']}"
        return "changed meanwhile; reload"

    # ---- delivery, called from the sync -----------------------------------------------------------------
    def tick(self, now):
        """Retire what can no longer happen. Cheap; called on every sync and panel refresh."""
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE reminders SET status = 'missed', reason = 'grace', finished_at = ?, updated_at = ? "
                        "WHERE status = 'scheduled' AND due_at + ? <= ?", (now, now, GRACE_S, now))
            con.execute("UPDATE reminders SET status = 'failed', reason = 'unconfirmed', finished_at = ?, updated_at = ? "
                        "WHERE status IN ('sent', 'received', 'presented') AND sent_at + ? <= ?",
                        (now, now, UNCONFIRMED_S, now))
            con.execute("DELETE FROM reminders WHERE status IN ('completed','dismissed','missed','failed','cancelled') "
                        "AND id NOT IN (SELECT id FROM reminders WHERE status IN "
                        "('completed','dismissed','missed','failed','cancelled') ORDER BY COALESCE(finished_at, 0) DESC LIMIT ?)",
                        (HISTORY_KEEP,))
            con.execute("COMMIT")
        self.sweep_orphans(include_temporary=False)            # audio of what just became missed/failed

    def reconcile_boot(self, boot, now):
        """Reminders handed to an earlier boot of the Stick with no result: the Stick restarted during delivery.
        Policy (bounded): one redelivery if still inside the grace window; otherwise failed. A restart the Stick itself
        reported as 'interrupted' (it was presenting) was already marked failed and is never retried."""
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = [self._row(r) for r in con.execute(
                f"{SELECT} WHERE status IN ('sent', 'received', 'presented') AND sent_boot != ?", (boot,))]
            for row in rows:
                if row["attempts"] < MAX_ATTEMPTS and now < row["due_at"] + GRACE_S:
                    con.execute("UPDATE reminders SET status = 'scheduled', reason = 'retry_after_restart', sent_at = NULL, "
                                "sent_boot = NULL, received_at = NULL, presented_at = NULL, delivered_mode = NULL, "
                                "updated_at = ? WHERE id = ? AND occ = ?", (now, row["id"], row["occ"]))
                else:
                    con.execute("UPDATE reminders SET status = 'failed', reason = 'restart', finished_at = ?, updated_at = ? "
                                "WHERE id = ? AND occ = ?", (now, now, row["id"], row["occ"]))
            con.execute("COMMIT")
        return len(rows)

    def next_for_device(self, now, boot):
        """The reminder to put in this sync reply, or None: first an unacknowledged one (a lost reply), else claim the
        next due one. One at a time, oldest due first, with a quiet gap after each, so a backlog never bursts."""
        self.tick(now)
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            open_row = self._get(con, "status IN ('sent', 'received', 'presented') ORDER BY sent_at LIMIT 1")
            if open_row:
                again = (open_row["status"] == "sent" and open_row["sent_boot"] == boot
                         and now - open_row["sent_at"] >= RESEND_AFTER_S
                         and now < open_row["due_at"] + GRACE_S)
                con.execute("COMMIT")
                return self._payload(open_row, now) if again else None
            last = con.execute("SELECT MAX(finished_at) FROM reminders WHERE sent_at IS NOT NULL").fetchone()[0]
            if last is not None and now - last < MIN_GAP_S:
                con.execute("COMMIT")
                return None
            row = self._get(con, "status = 'scheduled' AND due_at <= ? ORDER BY due_at, id LIMIT 1", (now,))
            if row is None:
                con.execute("COMMIT")
                return None
            mode = row["mode"]
            if mode == "speak" and row["audio"] != "ready":
                if row["audio"] == "pending" and now < row["due_at"] + AUDIO_WAIT_S:
                    con.execute("COMMIT")
                    return None                              # the audio is still being made: wait, in order
                mode, reason = "chime", "voice_unavailable"  # explicit, labelled fallback; never silently nothing
            else:
                reason = row["reason"] if row["reason"] == "retry_after_restart" else None
            changed = con.execute("UPDATE reminders SET status = 'sent', reason = ?, attempts = attempts + 1, sent_at = ?, "
                                  "sent_boot = ?, delivered_mode = ?, updated_at = ? "
                                  "WHERE id = ? AND occ = ? AND status = 'scheduled'",
                                  (reason, now, boot, mode, now, row["id"], row["occ"])).rowcount
            claimed = self._get(con, "id = ?", (row["id"],)) if changed else None
            con.execute("COMMIT")
        return self._payload(claimed, now) if claimed else None

    @staticmethod
    def _payload(row, now):
        return {"occ": row["occ"], "mode": row["delivered_mode"], "label": ascii_label(row["text"]),
                "ttl_ms": max(1, int((row["due_at"] + GRACE_S - now) * 1000)),
                "late_s": max(0, int(now - row["due_at"]))}

    # ---- what the Stick says ----------------------------------------------------------------------------
    def device_state(self, occ, state, boot, now):
        """A stage or result from the Stick (either its immediate report or a later sync). Moves only forward, and only for
        the boot the reminder was handed to. Returns True if anything changed."""
        if state not in DEVICE_STAGES and state not in DEVICE_RESULTS:
            return False
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._get(con, "occ = ?", (occ,))
            if row is None:
                con.execute("COMMIT")
                return False
            overridable = row["status"] in OPEN or (row["status"] == "failed" and row["reason"] in ("unconfirmed", "restart"))
            if not overridable or (row["sent_boot"] is not None and row["sent_boot"] != boot and state not in DEVICE_RESULTS):
                con.execute("COMMIT")
                return False
            changed = 0
            if state == "received" and row["status"] == "sent":
                changed = con.execute("UPDATE reminders SET status = 'received', received_at = ?, updated_at = ? "
                                      "WHERE occ = ? AND status = 'sent'", (now, now, occ)).rowcount
            elif state == "presented" and row["status"] in ("sent", "received"):
                changed = con.execute("UPDATE reminders SET status = 'presented', presented_at = ?, "
                                      "received_at = COALESCE(received_at, ?), updated_at = ? WHERE occ = ? "
                                      "AND status IN ('sent', 'received')", (now, now, now, occ)).rowcount
            elif state in DEVICE_RESULTS:
                status, reason = {"completed": ("completed", None), "muted": ("completed", "muted"),
                                  "dismissed": ("dismissed", None), "failed": ("failed", "device"),
                                  "media_failed": ("failed", "media"), "expired": ("missed", "late_on_device"),
                                  "interrupted": ("failed", "interrupted")}[state]
                changed = con.execute(
                    "UPDATE reminders SET status = ?, finished_at = ?, updated_at = ?, "
                    "reason = COALESCE(?, CASE WHEN reason IN ('voice_unavailable', 'retry_after_restart') THEN reason END), "
                    "received_at = COALESCE(received_at, ?) WHERE occ = ? AND (status IN ('sent','received','presented') "
                    "OR (status = 'failed' AND reason IN ('unconfirmed', 'restart')))",
                    (status, now, now, reason, now, occ)).rowcount
            con.execute("COMMIT")
        if changed and state in DEVICE_RESULTS:
            self._remove_audio(row)
        return bool(changed)

    def media_for(self, occ, now):
        """Path of the audio the Stick may download for this occurrence, or None (cancelled, edited, expired, not a
        spoken delivery). Downloading counts as the Stick having received it."""
        self.tick(now)
        row = self.get_by_occ(occ)
        if (row is None or row["status"] not in ("sent", "received") or row["delivered_mode"] != "speak"
                or row["audio"] != "ready" or now >= row["due_at"] + GRACE_S):
            return None
        path = self.audio_dir / audio_name(row["id"], row["occ"])
        if not path.is_file():
            return None
        if row["status"] == "sent":
            self.device_state(occ, "received", row["sent_boot"], now)
        return path

    # ---- audio assets (one worker, off the HTTP threads) ---------------------------------------------------
    def _remove_audio(self, row):
        try:
            (self.audio_dir / audio_name(row["id"], row["occ"])).unlink(missing_ok=True)
        except OSError:
            pass

    def sweep_orphans(self, include_temporary=True):
        """Delete audio that belongs to no live scheduled/open occurrence, and (at startup only) half-written temp files:
        while running, a temp file may be the audio being generated right now."""
        with closing(self.store.connect()) as con:
            live = {audio_name(i, o) for i, o in con.execute(
                "SELECT id, occ FROM reminders WHERE status IN ('scheduled', 'sent', 'received', 'presented')")}
        for path in self.audio_dir.glob("*"):
            if path.name not in live and (include_temporary or not path.name.startswith("tmp_")):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _requeue_audio(self):
        """After a restart: speech reminders still waiting for audio (or whose file vanished) are generated again."""
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = [self._row(r) for r in con.execute(f"{SELECT} WHERE status = 'scheduled' AND mode = 'speak'")]
            for row in rows:
                if row["audio"] != "ready" or not (self.audio_dir / audio_name(row["id"], row["occ"])).is_file():
                    con.execute("UPDATE reminders SET audio = 'pending', audio_tries = 0 WHERE id = ? AND occ = ?",
                                (row["id"], row["occ"]))
                    self.enqueue(row["occ"])
            con.execute("COMMIT")

    def enqueue(self, occ, delay=0.0):
        with self._jobs_lock:
            self._jobs.append([self._clock() + delay, occ])
        self._wake.set()

    def start_worker(self):
        if self._thread is None and self._synth is not None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="reminder-audio")
            self._thread.start()

    def _loop(self):
        while True:
            if not self.run_one():
                self._wake.wait(2.0)
                self._wake.clear()

    def run_one(self):
        """Generate the audio of the next due job. Returns False when there is nothing to do right now."""
        with self._jobs_lock:
            ready = [j for j in self._jobs if j[0] <= self._clock()]
            if not ready:
                return False
            job = min(ready)
            self._jobs.remove(job)
        self._generate(job[1])
        return True

    def _generate(self, occ):
        row = self.get_by_occ(occ)
        if row is None or row["status"] != "scheduled" or row["mode"] != "speak" or row["audio"] == "ready":
            return                                                    # edited, cancelled or already done
        waited = 0
        while self._busy() and waited < 120:                           # never compete with a voice question for the Mac
            self._sleep(1)
            waited += 1
        target = self.audio_dir / audio_name(row["id"], occ)
        temporary = self.audio_dir / f"tmp_{occ}.wav"
        try:
            self._synth(row["text"], temporary)
            size = temporary.stat().st_size
            if not 44 < size <= MAX_AUDIO_BYTES:
                raise RuntimeError(f"unusable audio ({size} bytes)")
            temporary.replace(target)
        except Exception as error:                                     # noqa: BLE001 - every failure is handled the same way
            temporary.unlink(missing_ok=True)
            self.audio_log.append((occ, f"error: {error}"))
            print(f"[reminder {row['id']}] voice generation failed: {error}", flush=True)
            self._audio_failed(row)
            return
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("UPDATE reminders SET audio = 'ready' WHERE occ = ? AND status = 'scheduled' "
                                  "AND mode = 'speak'", (occ,)).rowcount
            con.execute("COMMIT")
        if current:
            self.audio_log.append((occ, "ready"))
        else:                                                          # edited or cancelled while it was being made
            target.unlink(missing_ok=True)
            self.audio_log.append((occ, "discarded"))

    def _audio_failed(self, row):
        tries = row["audio_tries"] + 1
        final = tries >= AUDIO_TRIES
        with closing(self.store.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE reminders SET audio_tries = ?, audio = ? WHERE occ = ? AND status = 'scheduled'",
                        (tries, "failed" if final else "pending", row["occ"]))
            con.execute("COMMIT")
        if not final:
            self.enqueue(row["occ"], delay=20.0 * tries)

    # ---- the panel's view --------------------------------------------------------------------------------
    def view(self, now):
        self.tick(now)
        with closing(self.store.connect()) as con:
            rows = [self._row(r) for r in con.execute(f"{SELECT} ORDER BY due_at, id")]
        pending, history = [], []
        for row in rows:
            item = {"id": row["id"], "occ": row["occ"], "text": row["text"], "mode": row["mode"], "status": row["status"],
                    "reason": row["reason"], "audio": row["audio"], "delivered_mode": row["delivered_mode"],
                    "attempts": row["attempts"], **describe(row["due_at"])}
            if row["sent_at"] is not None:
                item["late_s"] = max(0, int(row["sent_at"] - row["due_at"]))
                item["late"] = item["late_s"] > LATE_AFTER_S
            if row["status"] in ("scheduled", *OPEN):
                item["overdue_s"] = max(0, int(now - row["due_at"]))
                pending.append(item)
            else:
                item["finished_at"] = row["finished_at"]
                history.append(item)
        history.sort(key=lambda i: i["finished_at"] or 0, reverse=True)
        return {"tz": TZ_NAME, "grace_min": GRACE_S // 60, "speech_available": self._synth is not None,
                "pending": pending, "history": history[:20], "max_text": MAX_TEXT}

    def preview(self, when, fold=None):
        now = self._clock()
        due = resolve_when(when, now, fold)
        return describe(due)
