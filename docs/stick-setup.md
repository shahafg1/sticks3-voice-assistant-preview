# Setting up a StickS3 after its files are installed (`tools/stick_setup.py`)

**Status:** implemented and tested against a **simulated** Stick and an **isolated** server only (`tests/test_stick_setup.py`). On a real Stick it has run **once, in check-only mode**, on the author's already-configured Stick (see "Hardware check"). **Its writing paths (`key` provisioning, `autostart --on/--off`) have never run on hardware**, and a fresh-device setup has never been done with the kit on hardware (see "What has not been tested").

```sh
python3 tools/stick_setup.py plan                                      # OFFLINE: the order, this Mac's key and server
python3 tools/stick_setup.py status    --port /dev/cu.usbmodemXXXX     # read-only: each step's state, kept apart
python3 tools/stick_setup.py wifi      --port /dev/cu.usbmodemXXXX     # bounded check on the Stick with its saved Wi-Fi
python3 tools/stick_setup.py key       --port … --expect-device ID --backup FOLDER
python3 tools/stick_setup.py autostart --port … --expect-device ID --on  --backup FOLDER   # or --off
python3 tools/stick_setup.py verify    --port … --expect-device ID     # restarts the Stick; waits for its first report
#   key --check / autostart --check   CHECK ONLY: compare or read, never write (not even when a backup is given)
#   wifi --reuse-only                 only use a connection the Stick already has: never switch Wi-Fi on or join
```

**Shared with the other Stick tools:**
* **Port and identity:** the port is always named explicitly. `--expect-device` is required for `key`, `autostart` and `verify`, and the supported firmware is required (UIFlow2 V2.4.8; docs/stick-backup.md).
* **Connecting stops the app** until the Stick restarts. `verify` restarts it itself; the other commands print the restart command.
* **A backup comes first:** anything that would write needs `--backup` with a complete backup of **this** Stick (`tools/stick_backup.py backup`). A run that writes nothing needs no backup.
* **Records:** every write leaves a private record in `<data_dir>/stick-setup/stick-<device id>/<operation>/record.json`. It never holds a key or a Wi-Fi value.

## The setup order

| # | Step | Where | Tool |
|---|---|---|---|
| 1 | Mac: configuration and the server running | Mac | docs/mac-setup.md |
| 2 | **Wi-Fi**, saved with UIFlow's Setup app **while the Stick still shows UIFlow's startup menu** | Stick | (by hand; see below) |
| 3 | A complete backup | Mac + USB | `stick_backup.py backup` |
| 4 | The assistant's files | Mac + USB | `stick_install.py preflight`, then `apply` (docs/stick-install.md) |
| 5 | The device key; then **restart the server** if this Mac's key is new | Mac + USB | `stick_setup.py key` |
| 6 | Auto-start (`boot_option` 0) | Mac + USB | `stick_setup.py autostart --on` |
| 7 | Verify: restart, first authenticated report from a new boot | Mac + USB | `stick_setup.py verify` |
| 8 | A short voice check: a silent recording, a question and answer, automatic sleep | Stick | by hand |

**Why this order:**
* **Wi-Fi comes first.** A fresh Stick shows UIFlow's startup menu (boot option 1, or not set), which is where the Setup app is. Once auto-start is on, the menu no longer appears.
* **Auto-start comes last.** `autostart --on` refuses until three things are true:
  * every installed file matches the install set;
  * Wi-Fi is saved;
  * the Stick holds this installation's key.
* **The server restart in step 5 is yours to do.** The server reads the key only when it starts. No tool here restarts it.

## Entering UIFlow's Setup app safely

**Where these instructions come from.** They come from UIFlow2 **2.4.8's published source** (`m5stack/uiflow-micropython`, tag `2.4.8`: `m5stack/modules/startup/__init__.py` and `startup/sticks3.py`). **They were not observed on hardware by this kit.** The one hardware fact is older: on 2026-11-18, the author saved Wi-Fi through the Setup app.

**On a fresh Stick** (boot option 1, or not set), UIFlow's startup menu appears after power-on. According to that source:
* **Buttons:**
  * press **BtnB** (the side button) to move to the next item;
  * **click BtnA** (the front button) to open it;
  * **double-click BtnA** to go back.
* **Menu items:** the launcher, Cloud, USB and **Setup**.
* **What Setup does:**
  * it starts an **open** Wi-Fi hotspot named `S3_` plus four characters of the chip's id;
  * join it with a phone; a captive page lists networks;
  * the name and password you choose are saved on the Stick.
* **"Server OK" on that screen means M5Stack's cloud,** not this Mac's server.

**Keep every Mac tool off the USB port while you use Setup.** In 2.4.8, saving prints the network name **and password** to the USB serial console. Nothing in this kit reads that console during Setup.

**On an installed Stick** (auto-start on), reach the menu without the button:
1. `stick_setup.py autostart --off --backup …` sets boot option 1. Every file, `main.py` included, stays.
2. Restart the Stick. UIFlow's startup menu appears; use Setup.
3. `stick_setup.py autostart --on --backup …`, then `verify`.

## Never hold BtnA while the Stick starts

**In 2.4.8, when the boot option isn't 1 and BtnA is held at start-up, UIFlow:**
* shows the menu;
* sets the boot option to 1;
* **removes `/flash/main.py`.**

This is `_apply_boot_button_override` in `startup/__init__.py`. Its source has a `FIXME: remove this file is temporary solution`. It doesn't call `commit()`, so **whether the 1 persists isn't established**. Either way, without `main.py` the assistant can't start by itself. The vendor `boot.py` comment presents holding BtnA as the way back to the menu. This kit never tells you to do it; it uses `autostart --off` instead.

**If it happened anyway** (tested in simulation; the button itself isn't simulated):
1. `stick_install.py preflight --port …` should show `main.py` as missing (to be created).
2. `stick_install.py apply --port … --expect-device … --expect-changes /flash/main.py` creates only `main.py`.
3. `stick_setup.py autostart --on --backup …`, then `verify`.

## Wi-Fi: on the Stick only
**What this kit never does:**
* ask for, store or read Wi-Fi credentials on the Mac;
* write them to the Stick;
* replace or clear what is saved.

**`wifi` is a bounded check that uses what's already saved, on the Stick.** It writes nothing and reports four levels separately:

| Level | How it's known | Unknown when |
|---|---|---|
| 1. Credentials saved | NVS `ssid0` / `pswd0` presence (values never leave the Stick) | the setting can't be read |
| 2. Network connected | The Stick joins with its saved values, within 20 s, and reports the Wi-Fi status code ("wrong password", "network not found" and so on) | the check doesn't finish |
| 3. Server port reachable | A TCP connection from the Stick to the address in its `/flash/device_config.json` (3 s). This is **not** authentication | the file is missing or unreadable |
| 4. Authenticated contact | **Only `verify`** (it needs the device key and the server) | n/a |

A failed level stops the ones after it ("not tried").

**Does `wifi` change the Stick's network state?**
* **By default, it may.** If the Stick isn't connected, it switches its Wi-Fi on and joins the saved network. That lasts until the Stick restarts, and nothing is saved.
* **With `--reuse-only`, never.** It only looks at an existing connection: no switching on, no joining. If there's no connection, it says so and stops.
* **Either way,** level 3 opens one short TCP connection to the server's port and closes it. If the Stick's configured address differs from this Mac's configuration, it says so. **A bounded check is not proof that the network stays up.**

## The device key
**Where it lives:**
* **On the Mac:** this installation's private secrets folder (`secrets_dir` in `config/host.conf`), file `device_key`, mode `0600` in a `0700` folder. A shared folder or key file is refused, and so is a secrets folder inside the project that git doesn't ignore.
* **On the Stick:** `/flash/device_key.txt`. The server and the Stick both ignore surrounding whitespace.

**Never shown.**
* The key is never printed, logged, put on a command line, or written to a record or backup. It reaches the Stick only as a file copy (`mpremote fs cp <path>`).
* Keys are compared with a **fresh random salt**: the Stick returns SHA-256(salt + key) for the Mac to compare with its own. Neither the key nor a reusable hash leaves either side.
* The simulated Stick flags any key or Wi-Fi text that would leave it, and tests require that it never does.

**What `key` does:**

| On the Stick | On this Mac | Result |
|---|---|---|
| none | none | **Backup required.** Creates this Mac's key once (atomic, `0600`, never overwritten). Copies it to `/flash/device_key.txt.part`, checks it **on the Stick**, then moves it into place only if no key exists. Then checks again |
| none | present | The same, with the existing Mac key |
| present, same | present | Nothing to do. **This is not pairing** |
| present, different | present | **Stops; nothing written.** Check `config/host.conf` points at the secrets folder the server uses. Rotation isn't implemented |
| present | none | **Stops; no key created.** The Stick was probably paired with another installation, or this one's secrets folder was lost or moved |

**Interruption.**
* If a copy is interrupted, or doesn't verify, `/flash/device_key.txt` is **not** created. Run `key` again: it **reuses the same Mac key** and replaces the partial copy.
* The partial copy is named like the key, so backups never copy it (they exclude every `device_key*` name) and restores never write it.
* `status` reports a partial copy left on the Stick.

**Rotation is not implemented.** Replacing a key on the Stick would cut off whatever else holds it; it's deferred to its own milestone. The older key tool (`provision_device_key.py`, not in the preview) is **retired**: it overwrote the Stick's key unconditionally and could pick the port by itself.

## Auto-start
* **`autostart` shows the current `boot_option` first.**
  * **0:** runs `main.py`, so the assistant starts by itself.
  * **1 or not set:** UIFlow's startup menu.
  * **2:** network only.
* **It changes only `boot_option`,** to 0 with `--on` or 1 with `--off`.
* **Before writing,** it records the previous value in the private record. It then writes and commits the value, and **reads it back**.
* **An already-correct value is a no-op** and needs no backup.
* **A write that fails or doesn't persist** is reported with the read-back value and the recorded previous one. It's never reported as set.
* **File backups don't contain this setting** (it's in NVS). The record holds its previous value.
* **Not verified until a boot is seen.** After `--on` the tool says "SET but NOT verified"; only `verify` can observe auto-start.

## Pairing: `verify`
**Before restarting, `verify` refuses unless:**
* the server's status can be read (`http://127.0.0.1:<panel_port>/api/status`);
* the server has a key loaded;
* the identified Stick holds this installation's key.

**Then it restarts the Stick** and waits (default 90 s) for a **fresh authenticated report from a new boot**. "New" means a boot ID that differs from the one before, with an uptime consistent with this restart.

**What a confirmed result means:**
* **Authenticated server contact:** confirmed.
* **Network connected:** yes, implied by the report.
* **Pairing:** confirmed **for this Stick, by timing**. Reports carry no device identity. The attribution rests on the restart of the Stick identified over USB.
* **Auto-start:** **observed** only if `boot_option` is 0.

**Otherwise it says "NOT confirmed"** and lists what's known and what isn't, with the next checks (`wifi`, and the server console for "authentication failed").

**Not done here:** the voice check (a silent recording, a question and answer, automatic sleep). It stays a physical check.

## Never done by this tool
* Ask for, write, read out or store Wi-Fi credentials.
* Print, log or commit a key, or put one on a command line; replace or delete a key on the Stick.
* Write any NVS value other than `boot_option`.
* Restart the server, or change any file other than the key file.
* Flash firmware.

## What has been tested
**`tests/test_stick_setup.py`** has 32 cases, run on a simulated Stick and the real `device_link.DeviceLink` on temporary folders as the server:
* the offline plan;
* read-only status that never counts a present or matching key as pairing;
* the Wi-Fi levels:
  * joined and reachable;
  * wrong password;
  * unreachable server;
  * a bounded timeout;
  * nothing saved.
* key provisioning:
  * the backup requirement;
  * creation;
  * private modes;
  * the key never in a call, an output or a record;
  * a repeated run that writes nothing;
  * a different key, and a Stick key with no Mac key, both stopping and preserving the key;
  * an interrupted and an unverified copy finished by reruns with the **same** key;
  * a shared secrets folder and the wrong device refused;
  * backups never copying a partial key.
* auto-start:
  * prerequisites;
  * the backup requirement;
  * set and read back, with the previous value recorded;
  * no-op;
  * `--off`;
  * a failed write;
  * a write that doesn't persist.
* `verify`:
  * no server, so nothing restarted;
  * a key the server hasn't loaded, then a server restart;
  * no report, including when a fresh report from the *previous* boot exists;
  * another key, so the Stick isn't restarted;
  * boot option 1, so auto-start isn't called observed.
* the whole order on a fresh simulated Stick;
* the reinstall path after BtnA removed `main.py`.

**Check-only modes (`key --check`, `autostart --check`, `wifi --reuse-only`)** are tested to write nothing, even when given a backup, and to never switch Wi-Fi on or join. Three more breakages (each mode ignored) were each caught.

**Breakage check:** twelve deliberate breakages of the tool were each caught:
* overwriting a different key;
* a new Mac key on every run;
* accepting any fresh report;
* no read-back;
* skipping the backup;
* skipping the partial-copy check;
* a Wi-Fi name, or the key, leaving the Stick;
* backups copying a partial key;
* restarting despite another key;
* auto-start without prerequisites;
* presence reported as pairing.

## Hardware check (check-only, once: 2026-11-19, the author's already-configured Stick)
**Authorized scope:**
* interrupting the app and reading the setup state;
* reading this installation's Mac-side key internally, for comparison only;
* one restart for `verify`.

No device or host configuration change was authorized, and none was made: no provisioning, no setting write, and no Setup app.

**Setup:**
* **Kit:** commit `b7c3101`.
* **Before connecting:**
  * the explicit port and the expected device id were confirmed;
  * the running server (the live checkout, one instance) has no override, so its secrets folder is the live installation's `secrets/`;
  * the folder is `0700`, the key `0600`, git-ignored, and long enough to use;
  * the server had a key loaded.

| Step | Command | Result |
|---|---|---|
| Offline plan | `plan` | No device contact; the live key path and server found |
| Status | `status` | Already installed; Wi-Fi name and password saved; key on the Stick and this Mac, **same key yes** (salted comparison under real MicroPython); `boot_option` 0; server key loaded; pairing "NOT confirmed" |
| Wi-Fi | `wifi --reuse-only` | Saved; connected (the existing connection **reused**: no switching on, no joining); status code 1010 shown as "connected (got an address)"; server port reachable at the address the Mac's configuration also gives |
| Key | `key --check` | Same key; nothing written |
| Boot option | `autostart --check` | 0, read only |
| Verify | `verify` | Restarted the identified Stick. A **fresh authenticated report from a new boot** arrived 10 s later, with uptime consistent with the restart. Auto-start observed (`boot_option` 0) |

**Audit.**
* **Commands sent:** 17: 16 read snippets and the one restart. There were no file copies, renames, removals or NVS writes.
* **The Wi-Fi snippet** was sent with `REUSE_ONLY = 1`. Its `active(True)` line is in the text but skipped.
* **No key text:** neither the key nor its plain SHA-256 appears in the logged commands, the tool outputs, or the private verification record. The comparisons carried only a one-time salted digest.

**Afterwards:**
* fresh authenticated reports every few seconds on the new boot;
* the USB port was released, with one server and no `mpremote` process;
* the server wasn't restarted.

**What this does and doesn't show:**
* **Pairing evidence is correlation, not direct device identification.** A new authenticated report began right after this tool restarted the Stick identified over USB, and its uptime fits that restart. Reports carry no device identity, so another device holding the same key and restarting at the same moment couldn't be told apart.
* **It doesn't exercise any writing path.** That includes key creation, copy or interruption, `autostart --on/--off`, and NVS writes and their read-back. It also doesn't cover a Stick that isn't already set up, or the default `wifi` join.

## What has not been tested
* **On a real Stick:** every writing path (`key` provisioning and its interruption handling, `autostart --on/--off`), the default `wifi` join, and a Stick that isn't already set up. Only the check-only run above has happened.
* **The device-side snippets under MicroPython**, beyond the read-only ones that ran above:
  * `esp32.NVS.set_u8` and `commit`;
  * Wi-Fi status codes other than 1010;
  * a `socket` timeout to an unreachable server.
* **UIFlow's menu and Setup app on this kit's hardware,** as described from source above.
* **A fresh-device setup.** No spare Stick or Mac is available. The kit's only hardware evidence is upgrades and checks of the author's existing installation. Further work doesn't wait for hardware: everything is built and tested in simulation, and labelled as such until a fresh device is available.
