# Desktop panel, milestone M1: design, protocol, rollback

Scope of M1: a local browser panel (Hebrew, right-to-left) that shows what the
Stick last reported and sets the screen brightness. No sleep, volume, reminders,
calendar or email yet, and no AI calls.

## Who does what

| Part | Responsibility |
|---|---|
| Panel (browser) | Controls and honest status. Holds no state of its own. |
| Mac server | Persists the desired brightness (SQLite), serves the panel on loopback, answers the Stick's sync requests. |
| Stick | Applies the setting, reads it back, reports its state, caches the last applied value. |

The panel is served by the same server process on `127.0.0.1:8001` (loopback only).
The device protocol stays on the existing LAN port 8000.

## Device status: what it can and cannot say

The Stick syncs only while it is idle and still (the same rule as notification
polling), so it **cannot report continuously**: during recording, waiting for an
answer, downloading, speaking, a shake reaction, a notification or the shake
cooldown there are no reports. The panel therefore never claims "connected" or
"disconnected". It shows the **age of the last authenticated report** and
classifies it:

| Age of last report | Panel says |
|---|---|
| none ever | No report received yet |
| up to 10 s | Fresh report |
| 10 s to 5 min | No report right now: the Stick may be busy or unavailable (an expected gap) |
| over 5 min | Stale information: unavailable or off, values shown are old |

Only authenticated reports count. A server-side voice job is never evidence of
connection; while the contact is in the "gap" class the panel may mention that a
job is being processed, strictly as a possible explanation for the gap.

Three kinds of information are always kept apart: **confirmed state** (values the
Stick reported, with their age), **pending changes** (desired but not yet
confirmed) and **stale/unavailable** information.

## Brightness: desired versus confirmed

- The slider sets the **desired** brightness (5 to 100 percent; below 5 would risk a
  dark, unusable screen). It is stored in SQLite with a global **revision** that
  increases by one per real change, next to a random **store id** created with the
  database.
- The panel throttles slider events (at most 4 per second) and always sends the
  newest value last. The server also rate-limits (429). Guarantee: **the latest
  value is eventually applied**. Intermediate values may be applied too.
- The Stick applies the desired settings whenever `(store id, revision)` differs from
  what it last applied, in either direction, so a restored older database also
  reconciles. It reads the value back with `getBrightness()` and reports
  `store`, `rev` and `brightness`.
- **Confirmed** means exactly: the latest report carries the same store id,
  revision and brightness as the desired state. It is an acknowledgement that the
  device applied the value, not a light measurement. `rev` matches but the value
  differs is shown as "the device applied a different value". Anything else is
  **pending**, and still pending after 30 s gets a visible "not confirmed" note.
- A change made while the Stick is offline or busy stays pending and is applied at
  its next sync. The server hints a 1 s poll interval for 20 s after a change (and
  while a change is unconfirmed, up to 60 s), otherwise 3 s.

### Reconciliation
- **Stick boot:** it applies its cached `(store, rev, brightness)` once the app
  starts, then syncs; a differing server state replaces it.
- **Server restart:** the desired value, revision and store id persist; the last
  report is persisted too (at most every 30 s), so the panel shows it with its real
  age (stale if old), never as fresh.
- **No desired value yet:** nothing is sent and the Stick keeps its own default.

## Protocol (Stick to server)

`POST /device/sync` with header `X-Device-Key` and a JSON body (at most 1 KB):
`{proto, boot, app, fw, store, rev, notif, brightness, battery, charging, rssi, uptime}`
(optional fields omitted when unavailable). Response:
`{store, rev, settings:{brightness?}, notification|null, poll_ms}`.
It replaces `GET /notify/next` for new firmware; `notif` is the same cursor, so
notifications keep their acknowledgement, expiry and deduplication.

### Authentication (no unauthenticated fallback)
- The device secret is 32 random bytes in `secrets/device_key` (mode 0600, ignored by
  Git) on the Mac and `/flash/device_key.txt` on the Stick. `tools/stick_setup.py key`
  provisions it over USB without printing it (docs/stick-setup.md; the older `provision_device_key.py` is retired). It is never shown in the UI
  or logs, and failures log only the client address.
- **Authentication failures never trigger an unauthenticated fallback.** On 401 or 403
  the Stick backs off 30 s and keeps trying to authenticate.
- **There is no legacy mode any more** (`feature/net-auth`, docs/security.md). Without a key file
  the Stick sends nothing; a server without `/device/sync` (404) is a failure with a back-off,
  never a switch to `GET /notify/next`, which the server no longer has.
- **Limitation:** the key travels in clear text over LAN HTTP. Anyone who can sniff the
  LAN can read and replay it. Its reach is small (it authorises status reports and
  receiving the settings and notification labels), the network is assumed trusted,
  and a challenge-response scheme is a later hardening step. Every other LAN route now needs
  the key as well (docs/security.md).

## Administration (loopback only)
The admin listener binds `127.0.0.1` and never a LAN address. Every request checks
the `Host` header (DNS rebinding); every POST also requires a matching `Origin`,
`Content-Type: application/json` and a per-process CSRF token (`X-CSRF-Token`, sent
by the page, constant-time compared). No cookies are used. Responses carry a strict
Content-Security-Policy, `no-store` and no CORS headers. If the port is taken or the
database cannot be opened, the panel is disabled and the voice assistant runs on.

## Files
Server: `settings_store.py`, `device_link.py`, `panel_server.py`, `panel/` (UI), edits to
`audio_server.py`. Stick: `stick/device.py` (new), `stick/notify.py` (request and
response hooks, partial-send handling), `stick/voice_send.py` (use `DeviceSync`).
Tools: `tools/stick_setup.py`,
`Voice Assistant Panel.command`.

## Rolling back to an older version
Not covered by the preview: the version-by-version rollback procedures are kept by the maintainers. To go back to the Stick files you had before installing, see docs/getting-started.md.

## Verification record (M1, 2026-11-18)

Measured on the real Stick (firmware V2.4.8) against the real server, not assumed:

- **Hardware APIs:** `Lcd.setBrightness`/`getBrightness` round-trip exactly (13, 64, 128, 255); the
  display default is 127 (about 50 percent). `Power.getBatteryLevel`/`isCharging` (about 1 ms each),
  `WLAN.status("rssi")` (0.4 ms) and `esp32.firmware_info()` work. Cache file write: 9 ms.
- **Poller `step()` over 3,453 idle steps (92 s):** mean 0.73 ms, median 0.14 ms, p99 26.3 ms, max
  33.0 ms; 39 steps over 20 ms (about one per poll) and 6 over 30 ms; none over 50 ms. There is **no**
  30 ms guarantee: one step per poll takes roughly 26 to 33 ms (the v0.5.0 poller measured 29.7 ms).
- **Brightness confirmation latency:** 1.3 s to 6.0 s after a change (idle poll 3 s, then the fast 1 s
  poll; the Stick must sync once to receive it and once more to report it).
- **Rapid drag (12 changes, 250 ms apart):** the Stick applied 2 intermediate values and the final
  one, which was confirmed 1.3 s after the last request. In a real browser, 30 slider events in 1.85 s
  produced 8 requests (4.3 per second), the last carrying the final value.
- **Reporting gap (Stick silent for 20 s):** status went fresh to "gap" after 10 s, never "disconnected";
  a change made during the gap stayed pending and was confirmed 3.0 s after the Stick resumed.
- **Authentication failure:** with a wrong key the Stick built exactly two requests in 38 s, both
  `POST /device/sync`, never a legacy `GET /notify/next`; the server logged the failure without the key.
- **Boot cache:** after wiping the display, a fresh `DeviceState` restored the cached brightness from flash.
- **Server restart:** the panel was back in 1.4 s showing the persisted report with its real age; the
  Stick synced again about 6 s later with the desired value unchanged.

Rollback rehearsed end to end on the real system: Stick rolled back with the script, v0.6.0 Stick with the
new server (works through legacy polling, panel never shows it as fresh), server switched to the v0.6.0 tag
(`/device/sync` 404, no panel), `--check` reported CONSISTENT, silence job and notification worked, then
forward again through the new Stick with the v0.6.0 server (drops to legacy on 404) to the final state.

Known limits: the connection backlog is Python's default of 5, so a burst of 10 simultaneous connections
resets some (also true of v0.6.0; real load is one Stick plus a browser tab). After a 404 the Stick retries
`/device/sync` every 10 minutes, so reset it after upgrading the server. Brightness is applied when the app
starts (after Wi-Fi), not during the boot screen.

## Milestone M2: volume controls and screen-only sleep (v0.8.0)

**Settings** (same desired/confirmed model, same store and revision as brightness): `voice_volume` and
`chime_volume` (0-100 %, 0 = mute), `idle_timeout_s` (0 = disabled, 30, 60, 300; default disabled) and
`sleep_mode` (`dim` or `off`; default `dim`). They are stored in `data/panel.db` and cached on the Stick
(`device_cache.json`, written only when a value actually changes).

**Volume.** Voice volume covers spoken answers and shake clips; chime volume covers notification chimes.
An unset volume keeps today's built-in level (voice raw 255 = 100 %, chime raw 70 = 27 %). Percent maps to raw
as `(p*255+50)//100`. The level is read at the *start* of each playback, so a change never interrupts audio
that is playing and takes effect on the next playback. At 0 % nothing is played at all (the hardware still
reports "playing" at volume 0, so the speaker is skipped); timing, the mouth animation and button-stop are kept.

**Screen-only sleep.** `awake -> face (2.5 s sleeping robot) -> dim (3 %) or off (backlight 0)`. Wi-Fi and
sync keep running; there is no MCU deep sleep. The awake brightness is kept separately and restored on
wake; boot is always awake. Brightness changes made while asleep are stored and applied on wake. Only real
activity (button, shake, notification, interaction) restarts the idle timer; polling, sync and panel
refreshes never do. Sleep never starts during recording, thinking, downloading, playback, a shake reaction or
an active notification.

**Commands.** "Sleep now" and "Wake" are one-shot commands in a `commands` table (id, type, expiry,
status). The server sends the *remaining* time (the Stick has no clock); ids at or below the highest seen are ignored
(at-least-once delivery, deduplicated on the Stick); the Stick reports the result *after* performing the action
(`executed`, or `expired` if it was busy past the expiry); an expired command is never delivered or replayed;
a reboot (new `boot` id) marks an unfinished command failed. Default expiry 45 s (`STICK_COMMAND_TTL_S`).
A command is not a desired state, so a local wake or a reboot cannot be undone by an old command. Commands
are only sent to a Stick that reports capability `sleep`.

**Buttons.** While asleep the first press only wakes the screen; the whole press (until release) is consumed
and a new press is needed to record. A deliberate shake wakes and runs the usual reaction. A notification wakes
the screen, plays its chime (if not muted), then the idle timer restarts. Motion history is reset on wake.

**Compatibility.** Capability reports are additive: an older Stick (v0.7.0) reports only `brightness`, the panel
disables the new controls and shows that the firmware is old. The database stays `user_version` 1 with
`meta.schema_minor` 2; before the first alteration an automatic copy `panel.db.pre-minor2-<time>.bak`
is made with the SQLite backup API. The real v0.7.0 `settings_store.py` and `stick/device.py` were tested against M2 data
and caches (both directions).

**Rollback to v0.7.0:** not covered by the preview (see "Rolling back to an older version").

### Verification record (M2, 2026-11-18)

Measured on the real Stick (USB, real main loop with only the button, shake, ports and time scripted;
isolated test server on port 8765, the real device key; the real desired settings were not touched):
- idle timeout 30 s: sleeping face at 31.6 s, dim at 34.2 s (the timer started about 1 s after boot);
- sync continued while dim and while the backlight was off (contact stayed "fresh", report age at most 3 s);
- the first press while asleep (held 4 s) woke the screen and did not record; a new press recorded and
  played the answer; the awake brightness (set while asleep) was restored on wake;
- manual sleep sent while recording was deferred and executed after the answer (63 s to 70.7 s, within the 45 s expiry);
  with a 6 s expiry the command expired and was shown "expired" (also when the Stick was not syncing); the Stick
  never slept afterwards;
- shake while asleep woke the screen and ran the reaction; a notification while asleep woke the screen and played the chime;
- `setVolume` calls and playback: answer at 25 % -> raw 64, muted voice -> no `playWav`, default voice 255, default
  chime 70, chime 10 % -> 26, chime 0 % -> no tones; the idle timer restarted after the notification;
- "off" mode: backlight 0 after the face; wake restored the saved brightness (77 raw for 30 %);
- hardware API: `setBrightness` and `setVolume` read back exactly.
Finding fixed during testing: an unrelated setting change rewrote the backlight; it is now written only when the value changes.

Panel in a real browser (simulated Stick on a throwaway server): sliders, mute label, selects, confirmation
badges, "Sleep now"/"Wake" lifecycle (pending, executed). The old-firmware gating is covered by the automated panel tests, not by a browser run.
Automated: 22 + 35 + 22 + 20 + 26 + 29 tests for the new modules plus the unchanged suites (conversation server 16,
conversation 20, hooks 19, notifications 16, poller 9, shake 13) all pass.

**Not measured / not done:** power consumption (no savings are claimed; Wi-Fi stays on, the dim face still lights the
screen slightly); the rollback to v0.7.0 was not rehearsed on the device; the Hebrew labels were not reviewed by the owner.

## Design feedback for the redesign (recorded, not yet implemented)

After using M1 the owner said the panel feels too much like a technical status page. The wanted
direction: a compact, playful **robot remote control** with clear buttons, a character, and minimal
text, with the diagnostic details (report age classes, store id and revision, firmware, signal,
uptime) moved into a **collapsed section**. Do not redesign yet; the status rules above stay as they
are, only their presentation changes.

> Note (v0.10.0): `panel/` is served directly from disk, so changing files there changes the live UI at once. The UI is now the robot remote described in [panel-ui.md](panel-ui.md); develop UI changes in a separate folder on an isolated server (`STICK_PANEL_DIR`).
