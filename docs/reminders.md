# One-time manual reminders (milestone M3, v0.9.0)

Created and managed in the local panel (Hebrew, right to left). Stored on the Mac, delivered by the Stick only
while it is idle. No recurrence, quiet hours, calendar/email access, voice-created reminders, cloud model or new paid
service. Routine controls make no AI calls: speech uses the same local Piper/phonikud TTS as answers.

## Using it
- **What / when / how.** Text (up to 200 characters), then either "in N minutes" (1 to 10080) or a local date and time,
  then the delivery mode. **Spoken delivery is the default** and is shown in the preview before saving; the other
  mode is a chime plus a generic on-device notification. The preview states the resolved time in Asia/Jerusalem with
  its UTC offset. There is no natural-language date parsing.
- **Edit / cancel** only while a reminder is still `scheduled`. Cancel is two clicks (no browser dialog). Once it was sent to
  the Stick it can no longer be changed (the panel says so); that is a deliberate honesty rule, not a bug.
- **History**: the last 20 finished reminders with plain statuses (below). The database keeps 100.
- **The Mac and the server must be awake** for on-time delivery. The Stick pulls reminders in its sync, so a sleeping Mac
  delivers nothing until it wakes.

## Time rules
| Case | Behaviour |
|---|---|
| Past time (or less than 5 s ahead) | rejected (`past`) |
| More than a year ahead | rejected (`too_far`) |
| Local time that does not exist (spring-forward gap) | rejected (`nonexistent`), never moved |
| Local time that happens twice (autumn) | rejected (`ambiguous`) with both candidates; the panel asks which one, then resends with `fold` |
| Late delivery | the Stick was busy or the server/Mac was down: delivered up to **30 minutes** after its time; labelled **late** if more than 60 s |
| Later than 30 minutes | marked **missed**, never spoken; **no catch-up summary** |
| Several overdue | strictly oldest-due first, one at a time, at least 15 s after the previous finished (no burst) |

## Statuses (what the panel shows)
`scheduled` (also while the voice is being made) -> `sent` (the server put it in a sync reply; **not** proof the Stick got it)
-> `received` (the Stick reported it, or downloaded the audio) -> `presented` (the Stick says it started) ->
**`completed`** (the Stick says the speech/chime ran to the end; reason `muted` if the volume is 0, so only the face was shown),
**`dismissed`** (the button stopped it), `failed` (reasons: `restart`, `interrupted`, `media`, `device`, `unconfirmed`),
`missed` (grace over, or it reached the Stick too late), `cancelled`. Receipt is never reported as playback.

## Delivery protocol (all authenticated with the device key)
1. `POST /device/sync` reply carries `reminder: {occ, mode, label, ttl_ms, late_s}` only if the Stick reports capability
   `reminder`, only when no Claude notification is in the same reply, and only when nothing else is open. An older Stick
   (v0.8.0, v0.7.0) never gets one and never consumes it (verified live: it stayed `scheduled`).
2. The Stick keeps it until the main loop is idle (never during recording, thinking, downloading, playback, a shake
   reaction or a notification), wakes the screen, then posts `received`, fetches `GET /device/reminder.wav?occ=N`
   (speech), plays it at the **voice volume** (chime reminders use the **chime volume**; 0 = muted), posts `presented`.
3. A fresh front-button press stops it; the whole press is consumed (a held press never starts a recording).
4. The result goes to `POST /device/reminder/state` at once **and** is repeated in every later sync (`rems`) until the server
   has it, so a lost report is not a lost result. The normal idle-sleep timer restarts afterwards.
5. The Stick shows only ASCII: the label is the ASCII part of the text (max 24 chars) or "Reminder". Hebrew stays in the panel
   and the spoken audio.

`occ` is the durable **occurrence id**: new on every edit, so audio, queued deliveries and acknowledgements of an old version can
never be mistaken for the current one. The Stick remembers the last 16 and ignores repeats; ids are not ordered by delivery
time, so it uses a set, not a high-water mark.

## Correctness and the crash windows (not exactly-once)
Every state change is one SQLite transaction guarded by the expected status and `occ`; delivery, edit and cancel racing each other
have exactly one winner (tested with threads). Delivery is **at-least-once with deduplication**, bounded:
- **Lost reply to the Stick** (it never got the reminder): offered again every few seconds until it reports; the Stick ignores repeats.
- **Lost report from the Stick** (it played it): results are re-sent in every sync; nothing is replayed.
- **Stick restarts while presenting**: a marker file (`/flash/rem_inflight.txt`, written only at presentation) makes the next boot report
  `interrupted`; the server marks it **failed and does not retry** (the user probably heard part of it).
- **Stick restarts after receiving but before the marker or any report** (milliseconds): the server sees a new boot id with no result and
  redelivers **once** if still inside the grace window; a second restart marks it failed. This can repeat an alert; it cannot repeat it twice.
- **Stick silent after delivery for 10 minutes**: `failed / unconfirmed`; a later true report still corrects it.
- **Unavoidable windows** (we do not claim exactly-once audible delivery): (1) a reset after playback ended and the marker was removed but before the
  immediate state report left the Stick (milliseconds, up to the 2 s report timeout): the result is lost with the RAM, the server sees a new boot with
  no result and redelivers once, so an alert that was already heard can be heard a second time (never a third); (2) a reset while presenting is reported
  `interrupted` and never retried, so a reminder that was cut short is not repeated; (3) a Mac or server crash is covered because the claim is committed
  before the reply is sent: afterwards the worst case is the at-least-once resend described above.

## Audio
One background worker makes the audio (never in an HTTP handler), **one TTS process at a time**, sharing a lock with voice answers and
waiting while a voice question is being processed. Each occurrence has its own file `data/reminders/r<id>_o<occ>.wav` (answer
files are never touched; reminders are not added to conversation memory). Edit/cancel/finish delete the old file; a generation that
lost a race with an edit discards its file; temp files are removed at startup. Failures retry (20 s, 40 s), then the status shows
"the voice could not be made" and, at the time, the reminder is delivered as a chime and labelled `voice_unavailable` (never silently
dropped). The server re-checks validity at claim time and again on every media request.

## Compatibility and rollback to v0.8.0
- Database: `user_version` stays 1, `meta.schema_minor` becomes 3, the `reminders` table is **additive**; an automatic sqlite-backup-API copy
  `data/panel.db.pre-minor3-<time>.bak` is made first. The real v0.8.0 `settings_store.py` opens the migrated database and edits settings and
  commands without touching reminders (tested); a v0.8.0 database migrates (tested, with the backup).
- **Rollback** to an older version: not covered by the preview (the maintainers keep those procedures).
- **Reminder data on rollback**: v0.8.0 ignores the table, so **pending reminders do not fire while rolled back**. They stay in the database
  and `data/reminders/`; after upgrading again, those inside the 30-minute window are delivered late, the rest are marked missed (never
  spoken). Cancel unwanted ones in the panel before rolling back (the script prints how many are pending). To drop all reminder data:
  restore the `.bak` made before the migration, or delete rows from `reminders`. Secrets and other runtime data are never touched.

## Verification record (2026-11-18)
Measured on the real Stick and real server unless noted. "Scripted" = the real Stick main loop with only a button press and time scripted, against
an isolated test server with the real TTS; the owner later confirmed one spoken reminder by hand (see Limitations).
- **Real pipeline** (live server, live Stick, real TTS, no scripting): a spoken reminder due in 1 minute: audio ready in ~2 s, delivered ~1 s after its
  time, `received -> presented -> completed`, the screen woke (it was dimmed by the idle timer).
- **Scripted**: stop button during ~8 s of speech stopped it (`dismissed`), the 3 s held press started **no** recording; a reminder while the screen was
  dimmed woke it, restored the brightness and played the chime at the chime volume (raw 102 = 40%), idle timer restarted (next sleep exactly 30 s after);
  voice volume 0 -> no playback at all, face shown, `completed/muted`; a reminder due while a question was being recorded waited until the Stick was idle
  again (delivered 12 s after its time, not interrupting).
- **Real reset during speech** (reset sent while `presented`): recorded `failed / interrupted`, attempts 1, never retried, never `completed`.
- **Real rollback rehearsal**: Stick files restored to v0.8.0 (hashes verified); the M3 server kept running; a due reminder stayed `scheduled` while the old
  Stick was in contact (not consumed); after redeploying v0.9.0 it was delivered (58 s after its time) and completed. The server half
  (`git switch --detach`) was not rehearsed; the v0.8.0 store code against the migrated database was tested instead.
- **Automated**: 41 (service) + 17 (Stick logic) + 43 (link) + 41 (real server over HTTP, incl. v0.8.0 migration, v0.8.0 store compatibility, restart, failing voice,
  security) + unchanged suites; browser check of the panel (preview, DST errors, create/edit/cancel).

## Limitations
No power or battery claims. The Stick screen cannot show Hebrew (ASCII label only). Delivery needs the Mac, server and Wi-Fi up. Time zone is fixed to Asia/Jerusalem.
Edit/cancel is impossible once sent. The 60 s "late" threshold and 30-minute grace are constants (`reminders.py`). Physically confirmed by the owner, exactly: a spoken reminder created through the panel worked; the front button stopped reminder playback without starting a recording; cancelling a pending reminder prevented it from playing. Nothing else is claimed as physically tested (see the README).
