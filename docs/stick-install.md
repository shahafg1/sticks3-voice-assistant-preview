# Installing the assistant's files on a StickS3 (`tools/stick_install.py`)

**Status:** implemented and tested against a **simulated** Stick (`tests/test_stick_install.py`). On a real Stick, `plan` and `preflight` ran read-only once, then `apply` ran once as an **upgrade of the author's existing v0.11.1 installation** (five files; see "Hardware upgrade"). **A first installation on a fresh Stick has never run on hardware**, and neither has recovery. It installs **files only**. A complete file installation does **not** mean the assistant is ready (see "What remains after a file installation").

```sh
python3 tools/stick_install.py plan                                       # OFFLINE: check every source, list the install set
python3 tools/stick_install.py preflight --port /dev/cu.usbmodemXXXX      # read-only: identify, compare, space, state
python3 tools/stick_install.py apply     --port /dev/cu.usbmodemXXXX      # write what is missing or different
#   --backup FOLDER         a complete backup of THIS Stick (tools/stick_backup.py); required when existing files would be replaced
#   --expect-device ID      refuse unless the Stick reports this device id
#   --expect-changes PATH   refuse before any write unless exactly these files would be written (repeatable)
#   --restart               restart at the end, only when that is safe
```

**Precondition:** a StickS3 on the supported firmware, **UIFlow2 V2.4.8, MicroPython 1.27.0, build 2026-06-26** (docs/stick-backup.md, "Supported firmware policy").
* Any other firmware is refused.
* An unidentifiable Stick is refused.
* This tool never flashes firmware.

## The install set: `stick/install_manifest.json`
One explicit list, in activation order. Every source file is **pinned by SHA-256**.

| Order | Files | Role |
|---|---|---|
| 1 | `/flash/device_config.json` | Generated on this Mac from `config/host.conf`: the server address and port only, no secret |
| 2–4 | `/flash/sounds/shake1.wav`, `shake2.wav`, `shake3.wav` | Shake reactions: original synthesized sound effects (MIT; `tools/make_sound_effects.py`) |
| 5–14 | `server_config.py`, `mic.py`, `statick_shapes.py`, `shake.py`, `voice_send.py`, `device.py`, `sleep.py`, `reminder.py`, `robot.py`, `notify.py` | The app modules, in `tools/deploy_stick.sh` order. `mic.py` is the **v0.11.1 microphone fix** (byte-identical to the v0.11.1 tag), and `statick_shapes.py` and `robot.py` are **Statick** |
| 15 | `/flash/main.py` | Runs the assistant at start-up, but only when `boot_option` is 0, which this tool never sets |

**Where files come from.** Sources come **only from this checkout**, never from a backup or a Stick.

**What `plan` refuses** (also `preflight` and `apply`), before any port is opened:
* a missing source file (for example a sound);
* a source that doesn't match its pinned hash;
* a symbolic link;
* an unsafe or protected destination;
* a device configuration that can't be generated (no `advertise_host`).

**Keeping the manifest current.** Tests check that the manifest lists exactly the modules in `deploy_stick.sh` (same order), every file in `stick/sounds/`, and the pinned hashes. After a reviewed change to a Stick file, re-pin with `python3 tools/stick_install.py pin`.

**Non-commercial use only.** The plan says so: the shake sounds are original effects (MIT), but the Hebrew voice that setup downloads is CC-BY-NC-4.0 (THIRD_PARTY_NOTICES.md).

## The three modes
| Mode | Opens the port? | Writes? | Reports |
|---|---|---|---|
| `plan` (default first step) | **No** (it has no `--port`) | Nothing | The install set, sizes, roles, licensing, the space a first installation needs, and what remains after installation |
| `preflight` | Yes, explicitly named | Nothing | Identity and firmware; each file as replaced / created / already matches / conflict; files outside the set (left alone). Also the **installation state**, the space needed versus free, and whether a backup is required |
| `apply` | Yes, explicitly named | Only install-set files that differ or are missing | Everything above, then the write stages, a private record and the remaining setup |

**Connecting stops the app.** `preflight` and `apply` stop a running app and leave the Stick at the MicroPython REPL (docs/stick-backup.md, "What connecting does").

**Installation state:**
* **"no assistant installed (first installation)":** none of the app modules are present. UIFlow's own `main.py`, `boot.py` and `README.md` don't count as an installation.
* **"existing installation":** some modules are present, from a full, older or partial installation.
* **"already installed":** every file matches.

## Before anything is written
* **Firmware and device:** the Stick is identified as the supported firmware, and as `--expect-device` if given.
* **Conflicts:** a folder where a file belongs, or a file blocking a needed folder, refuses the run. The tool never moves or deletes it.
* **A backup when anything existing would change.** If any existing file would be **replaced** (including UIFlow's own `main.py` on a fresh Stick), `apply` needs `--backup` with a **complete, verified** backup of **this** Stick. That backup must also hold the version **now** on the Stick of every file to be replaced. Otherwise it is refused: "take a fresh backup".
  * A backup isn't needed when only new files are created.
  * The backup is only checked, never used as a source.
* **Space.** Staging needs the sum of the files to write, plus 8 KB per file and 64 KB.
  * **What the margin is:** a fixed allowance, **not** computed from the Stick's block size. The 8 KB per file is meant to cover rounding each file up to whole blocks, and the 64 KB to cover the staging folders' metadata and leave a reserve. On the one Stick measured (LittleFS2, 4,096-byte blocks), that is at least one spare block per file plus 16 blocks.
  * **Free space** is read with `os.statvfs("/flash")` as free blocks × block size.
  * **"Would remain after installation"** in `preflight` is file-size arithmetic only; block rounding makes the real figure somewhat lower.
  * **First installation:** all 15 files are staged, about **473 KB**.
  * **Existing installation:** only the files that differ are staged.
  * **Replaced files** are moved aside, not copied, so they cost no extra space.
  * **The 572 KB check:** the author's Stick, an **existing** installation, reports **about 572 KB free**. That says nothing about a fresh Stick: its free space has never been measured, and the tool checks each Stick when it connects. The tool never assumes two full copies fit.
  * **If it doesn't fit,** nothing is written. **No file is ever deleted to make room.**
* **Sources:** every source is checked again just before the first write.

## Writing, interruption and resuming
It uses the same engine as `tools/stick_restore.py`, and the same staging area, `/flash/.stick-restore/<operation>/`. Previous versions go to `…/<operation>/prev/`. The step table there applies unchanged (docs/stick-restore.md, "What `apply` does"):
1. **Stage:** copy each file into the staging area and verify every staged copy **on the Stick**. Until then, no app file has changed, so a restart is safe.
2. **Activate:** move each current version (if any) aside, then move the staged copy into place, in manifest order with `main.py` last. This is **not atomic**: an interruption can leave old and new files mixed or a file missing. The tool then says **Do NOT restart**, and `--restart` is ignored.
3. **Verify:** check **every** file in the install set on the Stick by SHA-256. Only then does it print `FILES INSTALLED AND VERIFIED`.
4. **Clean up:** remove the operation's staging folder, and the empty staging area.

**Resuming.** Run `apply` again: files that already match are skipped, and the rest are staged, verified and activated.
* **Staged copies** left by earlier runs are removed.
* **Previous versions** from an earlier run's `prev/` are **kept and reported**. They may be the only copy of what was there, and they take space until you remove them.
* **Recovery isn't guaranteed.** Going back to the pre-installation state means restoring the backup and removing the files the installation **created** (see "Going back: the recovery procedure").

## Going back: the recovery procedure
Two steps, both limited to the files one installation touched. Nothing restarts the Stick until both are verified.

1. **Restore only the replaced files** from the backup taken before the installation:
   ```sh
   python3 tools/stick_restore.py compare BACKUP --port … --only /flash/voice_send.py --only /flash/device.py --only /flash/notify.py
   python3 tools/stick_restore.py apply   BACKUP --port … --only … (same three) --expect-changes <each file compare lists as differing or missing>
   ```
   `--only` keeps the restore to those files. Files the app rewrote since the backup (the settings cache, the last reply) are left as they are; without `--only` they would be restored too.
2. **Remove only what the installation created:**
   ```sh
   python3 tools/stick_install.py remove-created <the installation's operation folder> --port … \
       --expect-remove /flash/device_config.json --expect-remove /flash/server_config.py
   ```
   * It reads the files that installation's record lists as **`create`**. `--expect-remove` must name exactly those, or it refuses before connecting.
   * Each file is removed **only if it still has the exact SHA-256 the installation wrote**. If any differs, **nothing** is removed, and the tool says to look at it yourself.
   * It verifies that the files are gone and notes the step in the record. It never restarts the Stick.
   * This is the installer's only deletion. It never touches anything else, including the staging area: previous versions kept there by an interrupted run stay, and are reported.

**Order:** restore first, then remove. v0.11.1's modules don't use either created file, so the pre-installation app starts once both are done.

**Tested in simulation** (`tests/test_stick_install.py`, on a simulated v0.11.1 Stick):
* after a complete upgrade, and after the app rewrote its cache;
* after an activation interrupted with `voice_send.py` moved aside (missing);
* refusal of a partial or wider `--expect-remove` and of a changed created file;
* `--only` refusing a file the backup doesn't hold, and leaving other changed files alone.

Five deliberate breakages of the recovery code were each caught. **On hardware it is untested** unless a real recovery is recorded below.

## The operation record
`<data_dir>/stick-installs/stick-<device id>/<operation>/`, private (`0700`/`0600`), outside git.

**`record.json` holds:**
* status, stage and `safe_to_restart_app`;
* the install manifest's hash and the kit's git commit;
* the device identity (presence-only flags);
* the installation state before;
* the backup used;
* for each written file: **`create` or `replace`**, size, hash and its stage flags;
* files left on the Stick;
* **earlier unfinished installations on this Stick, with the files they created and replaced**, so recovery can account for newly introduced files as well as replaced ones;
* `assistant_ready: false`.

**Also in the folder:** the generated `device_config.json` used as this run's source. File contents are never logged.

## What remains after a file installation
`FILES INSTALLED AND VERIFIED` is the file installation only. The tool then lists what remains, none of which it does or checks:
* **Wi-Fi:** whether a network name and password are saved, presence only, as observed. Whether they work is not checked.
* **Device key:** present or absent, as observed. Provisioning and pairing with this Mac are not checked (`tools/stick_setup.py key`, then `verify`; docs/stick-setup.md).
* **Auto-start:** the observed `boot_option`. Only 0 starts the assistant by itself; this tool never changes it.
* **Mac server configuration:** the address the Stick is now pointed at. Whether the server runs and is reachable there is not checked.
* **Claude login on the Mac:** not checked.
* **End-to-end voice check:** not done.

## Never done by this tool
* Flash firmware, or write the bootloader, partition table or any NVS value (Wi-Fi, `boot_option`).
* Write, replace or delete the device key.
* Change or delete any file outside the install set: the settings cache, user files, or UIFlow's `boot.py`, `README.md` and `apps/`.
* Copy anything from a backup or a Stick.
* Claim atomicity, guaranteed recovery, boot safety mid-activation, or that the assistant is ready.

## Hardware check (read-only, once)
**2026-11-19, the author's Stick** (supported firmware, v0.11.1 app files, `boot_option` 0), with the owner's authorization to stop and restart the app. **No installation was authorized or done:** this validates identification, comparison and the space check only, **not** installation, staging, interruption handling or recovery.

**Steps:**
1. `plan` (offline): all 14 file sources matched their pinned hashes (checked again independently), and `mic.py` is byte-identical to the v0.11.1 tag.
2. A per-file prediction was written down before connecting, from `git` (`main` versus this branch).
3. `preflight --port … --expect-device …` took about 6 s. Identity, the supported firmware and the expected device were confirmed.
4. One extra read-only `os.statvfs` read gave the block geometry.
5. Restart: the Stick was back with a fresh authenticated report on a new boot about 12 s after the restart command, and the port was released.

**Every command sent to the Stick was logged and checked:** six calls, all read-only, apart from the final `machine.reset()`. The only NVS access was `get_u8`/`get_str`.

**Result: the preflight matched the prediction exactly.**
* **State:** existing installation (9 of 10 modules present, 6 identical).
* **To be replaced:** `voice_send.py`, `device.py`, `notify.py`.
* **To be created:** `device_config.json`, `server_config.py`.
* **Already matching:** the other 10, including the three sounds, `mic.py`, `statick_shapes.py` and `main.py`.
* **Left alone:** 20 files outside the set (UIFlow's files, the settings cache, the author's own files).
* **Backup:** reported as required.
* **Space:** 5 files need about 146 KB of staging allowance; 572 KB free (143 of 446 blocks of 4,096 bytes, LittleFS2).

**Change from this check:** `preflight` now says the "would remain" figure ignores block rounding and that the staging allowance is a fixed margin.

## Hardware upgrade (2026-11-19, the author's Stick, with the owner's authorization)
**What this was:** an **upgrade of an existing v0.11.1 installation**, limited to five files. It was **not** a first installation on a fresh Stick, and recovery was prepared but **not needed or exercised**.

**What was deployed:**
* **Kit:** commit `db3f642` (branch `feature/stick-install`).
* **Manifest:** `stick/install_manifest.json`, SHA-256 `98c68c95…2ebcf2`.
* **Unchanged:** `stick/` is the same as at `d4f9b68`.

**Before writing:**
1. **Compatibility with the running v0.11.1 server.**
   * Source review: the three modules change only where the server address comes from (`device_config.json`, read by `server_config.py`, instead of a built-in address). No request, endpoint or authentication changed, and the generated file holds the same address and port.
   * Isolated test: v0.11.1's own suite, with the server code byte-for-byte v0.11.1 and these Stick files overlaid, passed 21/21. That includes device link (43), device sync (26), the poller and the conversation server. The one test change needed was test plumbing that this branch already carries.
2. **Unchanged files:** `mic.py` (the microphone fix), `statick_shapes.py`, `robot.py` and the sounds are byte-identical to the v0.11.1 tag.
3. **Recovery prepared and tested in simulation first** ("Going back: the recovery procedure", commit `db3f642`).
4. **A fresh private backup:** 33 files, all verified against the Stick, device key excluded, 95 s. It holds the v0.11.1 version of each file to be replaced.
5. **`preflight` recheck:** identity, supported firmware, 572 KB free, and exactly the five-file change set.

**The installation:** `apply --backup … --expect-device … --expect-changes` (the five files), without `--restart`.
* It took 20 s and printed `FILES INSTALLED AND VERIFIED: 5 file(s) written and all 15 files verified`.
* **Created:** `device_config.json`, `server_config.py`. **Replaced:** `voice_send.py`, `device.py`, `notify.py`.
* No staging was left over.

**Independent checks before restarting:**
* **Installer preflight:** "already installed" (15 of 15 match).
* **`stick_restore.py compare` against the fresh backup:** exactly the 3 replaced files differ, the other 30 files are identical, and the only additions are the 2 created files.
* **Free space:** 572 → 568 KB.

**Audit:** every command sent to the Stick was logged (65 calls).
* The only copies to the Stick were the five staged files, inside the staging folder.
* Activation moved exactly those five files into place, and cleanup removed only this run's staging folder.
* The device key was only checked for existence (`os.stat`), never read or written. NVS was only read.

**After the restart:** a fresh authenticated report on a new boot about 12 s after the restart command, the USB port was released, and the server was not restarted.

**Result: a successful upgrade of the existing installation.**

**Physical check: the owner's confirmation** (what the owner saw and heard):
1. **Silent recording:** the Stick showed "No speech".
2. **Spoken question:** the Stick played a spoken answer.
3. **Left untouched for two minutes:** it dimmed by itself, with no restart or error observed.

**What the server's status establishes** (`/api/status`, polled each second during the check). It holds device reports and the screen state, not conversation content or audio, and the server's own console wasn't read:
* **No restart:** the same boot from the restart after the upgrade through the end of the check, about 22 minutes, with uptime consistent with that one boot.
* **Automatic sleep:** every sleep went `awake` → `face` → `dim` with no command sent, about 60 s after the last activity (the configured idle timeout is 60 s, in dim mode). This happened three times, including once by itself after start-up.
* **Two recordings:** two short gaps in the Stick's reports while it was busy, about 20 s apart, then reports resumed.
* **One voice job:** active on the server for about 9 s, after which reports resumed.
* **Not established:** the "No speech" outcome, the transcript and the spoken answer itself. Those rest on the owner's confirmation alone.

**What this does and doesn't show:**
* It shows the upgraded app reads its server address from `device_config.json`, reaches the v0.11.1 server, records, gets an answer and sleeps.
* It is one upgrade of one existing installation. It is **not** a fresh-device installation, and not a test of recovery, which wasn't needed.

**Limits:**
* One upgrade of one Stick; it says nothing about a fresh Stick.
* Recovery (`stick_restore.py --only` and `remove-created`) is tested in simulation only.
* Interruption handling and resuming on hardware are untested.

## Verification so far (simulated)
`tests/test_stick_install.py` has 21 cases. The simulated Stick starts in one of four states: a fresh UIFlow Stick, the same without UIFlow's `main.py`, a current installation, or an **old installation built from the v0.11.0 tag's files**. That last one has the pre-fix microphone code (`del warmup`) and no `mic.py`.

**Covered:**
* the manifest against the deploy list, sounds and pinned hashes;
* the microphone fix and Statick in the set;
* the offline plan, which makes no device contact;
* refusal of a missing asset, a changed source, or no server address;
* explicit ports;
* first-installation and old-installation preflight, which writes nothing;
* unsupported or unidentified firmware;
* a first installation, with every file created, the right order, private record modes, and "not ready" reporting;
* the backup requirement for replacing UIFlow's `main.py`;
* an old installation ending byte-identical to v0.11.1's `mic.py`, with the key, settings cache and Wi-Fi untouched;
* refusal of stale, foreign and damaged backups;
* a current installation and repeated runs, which write nothing;
* low space, refused with nothing deleted;
* conflicts;
* an interrupted transfer, then a rerun, with the record listing the earlier run's created and replaced files;
* an interrupted activation that refuses a restart, then a rerun;
* a run killed during activation;
* a final-hash failure, never reported as installed;
* the `--expect-changes` and `--expect-device` guards.

**Breakage check:** seven deliberate breakages of the tool were each caught:
* no backup required;
* a stale backup accepted;
* pinned hashes ignored;
* an old installation seen as a first installation;
* readiness claimed;
* `main.py` not last;
* the expected device ignored.

**Not verified:** anything on a real Stick beyond the read-only check and the one five-file upgrade above; the time to copy about 300 KB over real USB; a real first installation on a fresh UIFlow Stick, including how its `boot_option`, files and free space look; the app starting after an installation.
