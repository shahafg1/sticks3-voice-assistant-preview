# Network security of the distribution version

**Status:** implemented on `feature/net-auth` and tested against isolated servers, simulated Sticks, synthetic keys and stand-in AI (`tests/test_net_auth.py` and the updated suites). **Deployed once, on the author's installation (2026-11-19):** server commit `9249de5`, plus the two Stick files. See "Live deployment". It is not merged, tagged or released.

## What this protects, and what it doesn't
* **Protects:** a device on the same Wi-Fi can no longer submit audio (and so cause Whisper, Claude and TTS work, and spend the user's Claude usage), read job states, download spoken answers, or read or consume notification labels, without the device key.
* **Doesn't encrypt anything.** The Stick and the Mac talk **plain HTTP on the LAN**.
  * Anyone who can observe that traffic can read the audio, the answers, the notification labels **and the device key**, then replay the key.
  * Authentication over plain HTTP stops *unauthenticated* LAN clients. It doesn't stop an eavesdropper on the same network.
* **Not for the internet.** Never forward these ports through a router, expose them with a tunnel, or run the server on an untrusted network (a café, a hotel, a guest network shared with strangers).
* **The trust model:** a home LAN where every device is trusted not to sniff traffic. The Mac's own user account is trusted (any local process can reach the loopback listener).

## Route inventory
**Device port** (`device_port`, default 8000), on the LAN address the Stick uses:

| Route | Caller | Exposes data / triggers work | Before | Now |
|---|---|---|---|---|
| `POST /audio` | Stick `voice_send.py` | **Triggers** speech detection, Whisper, Claude (the user's account and connectors) and TTS | open | **key**; size ≤ 1,000,000 bytes and even; the key is checked **before the body is read**; rate and queue limits |
| `GET /status?id=` | Stick `voice_send.py` | Exposes a job's state and error detail | open | **key**; `id` required |
| `GET /reply.wav?id=` | Stick `voice_send.py` | Exposes the **spoken answer** (private content) | open; without `id` it waited for, and returned, the **latest** answer | **key**; `id` required; the "latest" fallback is **removed** |
| `GET /notify/next?after=` | old Stick code (legacy fallback) | Exposes **and consumes** notification labels (project names) | open | **removed (404)**; notifications reach the Stick only through `/device/sync` |
| `POST /notify` | Claude Code hook | Injects notifications | from this Mac only (address check) | **removed (404)**; moved to the loopback panel listener |
| `POST /device/sync` | Stick `device.py` | Settings, commands, reminders, notifications; the Stick's report | key | key, now checked **before the body is read** |
| `POST /device/reminder/state` | Stick `voice_send.py` | Reminder outcomes | key | key, checked before the body is read |
| `GET /device/reminder.wav?occ=` | Stick `voice_send.py` | A reminder's audio | key | key (unchanged) |
| anything else | n/a | n/a | 404 | 404 |

**Panel port** (`panel_port`, default 8001), bound to **127.0.0.1 only**:

| Route | Caller | Protection |
|---|---|---|
| `GET /`, static files, `GET /api/status` | the browser on this Mac | Host header check (DNS rebinding); CSP and no-store headers (unchanged) |
| `POST /api/...` (settings, commands, reminders) | the panel page | Host, Origin, `Content-Type: application/json`, a per-process CSRF token, and a rate limit (unchanged) |
| `POST /hook/notify` (**new**) | `hooks/stick_notify.py` | Loopback only; Host check; **no** `Origin` header allowed; `Content-Type: application/json` and `X-Stick-Hook: 1` required, so a web page would need a CORS preflight that is never granted; ≤ 2,048 bytes; rate-limited; accepts only `{kind, label, session, prompt}` |

**Other network clients:**
* `tools/stick_setup.py` and the setup wizard read `GET /api/status` on loopback.
* `stick_setup.py wifi` opens one TCP connection from the Stick to the device port and sends nothing.
* `tools/preflight.py --probe-server` only opens a TCP connection.

## The authentication approach
**The key.** It's the existing device key: 32 random bytes in hex, in this installation's secrets folder (`0600`) and in `/flash/device_key.txt` (docs/stick-setup.md). The Stick sends it in the `X-Device-Key` header on **every** request.

**One check for every route.** `device_link.KeyGate` loads the key once at start-up, compares in constant time, and locks a client address out of **all** routes after 20 failures in a minute (`429`, even with the right key). The audio server and the panel's device link share one gate.

**No key, no service.**
* A server without a key refuses every Stick request and says so at start-up ("device key NOT provisioned").
* A Stick without a key sends **nothing**, and shows "No device key" when the button is pressed.

**No unauthenticated fallback anywhere.** The Stick no longer drops to `GET /notify/next`, either without a key or when the server lacks `/device/sync` (404). That is now a failure with a back-off, reported once ("update the server").

**Secrets stay out of:**
* URLs: a `key=` query parameter is ignored and refused;
* command lines, and logs (failures log only the client address);
* responses, and Git.

Tests scan the server log and every response for the key.

## Limits
| Limit | Value | Where |
|---|---|---|
| Audio body | ≤ 1,000,000 bytes, even; otherwise `413`/`400`, unread | `audio_server.py` |
| Missing `Content-Length` | `411` | device port, hook |
| Sync body / reminder state body | ≤ 1,024 / ≤ 256 bytes (`413`) | device port |
| Hook body | ≤ 2,048 bytes | panel |
| Upload rate | 6 per minute sustained, a burst of 3 (`429`, `Retry-After`) | `STICK_UPLOADS_PER_MIN` |
| Waiting or running recordings | 2; more are refused (`503`) and **not queued** | `STICK_MAX_PENDING_JOBS` |
| Reading a request line, headers or body | 15 s, then the connection is dropped | `STICK_REQUEST_TIMEOUT_S` |
| Concurrent device-port connections | 16; more are closed at once | `audio_server.py` |
| Failed keys | 20 per minute per client, then `429` on every route | `device_link.py` |
| Claude per question | 120 s (unchanged) | `audio_server.py` |

**Order of the checks:** a rejected request (no or wrong key, locked out, bad size, rate, queue) never enqueues a job, writes a recording, or invokes speech detection, Whisper, Claude or TTS. The tests check this from stand-in call logs and the recordings folder.

## Compatibility and migration
**Combinations:**

| Stick | Server | Works? |
|---|---|---|
| new (this branch) | new | yes |
| new | old (v0.11.1, live today) | yes: the old server ignores the extra header, and has `/device/sync` |
| old (v0.11.1 and earlier) | new | **no**: uploads get `401` ("Server status: 401" on the screen); notifications stop (no `/notify/next`). Settings and reminders still work through the keyed sync |
| any, with no key | new | **no**, by design |

**Old Claude Code hooks** installed with this project call `hooks/stick_notify.py` from the project folder, so they follow the code. A user who set `STICK_NOTIFY_URL` to the old device-port URL gets nothing (`404`) until they remove or update it.

**Migration order** (for a future deployment; not done):
1. a backup;
2. **the Stick first** (it works with both servers);
3. then the server;
4. then the hook (no action unless `STICK_NOTIFY_URL` is set).

Compatibility is never bought by weakening authentication: there's no switch to re-open the old routes.

## Remaining security limits
1. **No encryption:** plain HTTP on the LAN, so audio, answers, labels and the key can be sniffed and the key replayed on the same network. TLS or a challenge-response scheme is future work.
2. **One shared key** for the Stick. There's no per-request signature, no nonce and no rotation (key rotation isn't implemented).
3. **Lock-out by client address** only. An attacker with many addresses gets 20 guesses per address per minute. The key space (256 bits) makes guessing irrelevant, but the lock-out can be used to deny service to the Stick from its own address only if the attacker can spoof it.
4. **Local processes** on the Mac can reach the loopback listener and post notifications. The Mac's user account is trusted.
5. **Rate limits are global** (one Stick per installation): a flood with the right key can still use the quota.
6. **`/api/status` on loopback** shows device facts to any local process (unchanged).
7. **Not reviewed** by anyone other than the author. There's no fuzzing, and no review of the Claude tool permissions (read-only Gmail and Calendar tools are allowed).

## Live deployment (2026-11-19, the author's installation, owner-authorized)
**Before:**
* **The full server difference was reviewed:** authentication, plus the inherited host configuration (locations now come from `config/host.conf`). There's no database or panel UI change, so a rollback needs no migration.
* **Every location** resolved to the existing folders. With no `whisper-model`, the server uses the cached Whisper model as before.
* **The hook migration needed no settings change.** The installed hooks run the project's `hooks/stick_notify.py` with `voice-env` Python `-I -S`, and no `STICK_NOTIFY_URL` was set. The new script, run exactly that way against a probe, posted `/hook/notify` with its header. After a rollback the old script returns with the checkout.
* **Preserved privately:** the owner's untracked internal audit document, moved and copied with matching hashes before the checkout (the branch tracks a different version), a copy of the panel database, the exact old server version, and the rollback steps.
* **A fresh Stick backup** (35 files, key excluded). The mic fix and Statick files are unchanged.

**Deployment:**
1. **Stick:** only `voice_send.py` and `device.py` were installed, with `--expect-changes` enforced. All 15 install-set files were verified, and a comparison against the backup showed exactly those two differing.
2. **Compatibility with the old server:** after a restart, a fresh authenticated report came from a new boot. The old server accepted the new request shape (an upload with the key header got 200, status `no_speech`, reply 204).
3. **The server switch:**
   * the old server was stopped with SIGINT;
   * the live checkout was moved to `9249de5`, detached;
   * `config/host.conf` was written by `stick_config.py init --advertise-host … --keep-existing-locations`, and preflight reported nothing required missing;
   * the server was started with the usual launcher: one instance, the same addresses, the key loaded.

**Observed (hardware and server):**
* **Contact:** a fresh authenticated report 2 s after the server started.
* **Without the key:** `POST /audio` and `GET /status` got `401`, and `/notify/next` got `404`. The recordings folder didn't change, so no job ran. Only three refusals, all from the Mac's own address: the Stick was never locked out and nothing was flooded.
* **Notifications:** real `claude -p` runs fired the installed Stop hook, and the new server answered `queued`. The Stick acknowledged each through its authenticated sync, and its screen woke for the scheduled test.
* **Voice question:** a voice job ran for about 8 s, followed by a fresh report. Since the server accepts only keyed uploads, the question itself went through the keyed `/audio`, `/status` and `/reply.wav`.
* **Sleep:** the screen dimmed with no command, about 60 s later, on the same boot (no restart).

**Confirmed by the owner:**
* **The notification** was seen on the Stick, with the chime heard (the scheduled test). Two earlier ones were acknowledged by the Stick but shown before the owner was watching.
* **A normal voice question** got its full spoken answer.
* **Left for about two minutes,** the screen dimmed by itself, with no restart or error.

**Not covered on hardware** (kept in the isolated tests on purpose): wrong keys from the Stick, lock-out, the rate, queue, size and timeout limits, and an old Stick against the new server.

**Rollback** (prepared, not needed; the steps are in the private deployment record):
1. Stop the server.
2. Move `config/host.conf` aside.
3. `git checkout main`.
4. Restore the untracked handoff file.
5. Start the old server.
6. Only if needed, restore the two Stick files from the pre-deployment backup (`stick_restore.py --only`).

**Returning to the old server reopens its unauthenticated LAN routes.**

## Bounded deployment plan (as prepared before the deployment above)
1. **Prerequisites:**
   * merge order decided;
   * a fresh backup (`stick_backup.py`);
   * the live server's current commit recorded;
   * the Mac's key present and the Stick's key matching (`stick_setup.py key --check`).
2. **The Stick first:** `stick_install.py apply --backup … --expect-changes /flash/voice_send.py --expect-changes /flash/device.py`. Then `verify`, and a voice check against the **old** server: it must still work.
3. **The server second:** stop the live server, switch the live checkout to the reviewed commit, start it, and check:
   * a fresh authenticated report;
   * one voice question;
   * a Claude Code notification reaching the Stick;
   * `curl` without a key getting `401`.
4. **Rollback:**
   * the server: the previous commit, restarted;
   * the Stick: `stick_restore.py --only` the two files from the backup. A new Stick works with the old server, so the server alone can be rolled back first.
