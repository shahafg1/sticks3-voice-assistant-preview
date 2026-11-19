# Restoring files from a Stick backup (`tools/stick_restore.py`)

*Its write engine and staging area (`/flash/.stick-restore`) are shared with the first-install file deployment, `tools/stick_install.py` (docs/stick-install.md).*

**Status:**
* **Simulated:** everything, tested against a simulated Stick with synthetic backups (`tests/test_stick_restore.py`).
* **Real hardware, once:** a **single-file** restore of `/flash/README.md`, a file the app never uses, on the author's Stick (2026-11-19; see "Single-file hardware restore test").
* **What that is not:** proof of multi-file recovery, of replacing app files, or of safety after an interruption or a power loss. All of those are simulated only.

```sh
python3 tools/stick_restore.py plan    BACKUP                                    # OFFLINE: check the backup, list what it holds
python3 tools/stick_restore.py compare BACKUP --port /dev/cu.usbmodemXXXX        # read-only: identify the Stick, compare every file
python3 tools/stick_restore.py apply   BACKUP --port /dev/cu.usbmodemXXXX        # write the files that differ or are missing
#   --restart   (apply only) restart the Stick at the end, but only when that is safe (see "When it is safe to restart")
#   --expect-changes PATH   (apply only, repeatable) refuse before any write unless exactly these files would be written
#   --only PATH             (compare and apply, repeatable) only these backup files; every other file is left as it is.
#                           A name the backup doesn't hold refuses the run before connecting
#   BACKUP is one folder made by stick_backup.py: <data_dir>/stick-backups/stick-<id>/<time>-<random>
```

## The three modes
| Mode | Opens the port? | Writes? | What it tells you |
|---|---|---|---|
| `plan` (the default first step) | **No.** It has no `--port` option at all | Nothing, and no record either | Whether the backup is restorable, its files and sizes, the exclusions, and what is never restored. **It is not a comparison with any device**: it can't say which files would change |
| `compare` | Yes, explicitly named | Nothing | The Stick's identity and firmware, then each backup file as *replaced*, *created*, *already matches*, or a *conflict*. Also files on the Stick that aren't in the backup (they stay), and leftovers from an earlier interrupted restore |
| `apply` | Yes, explicitly named | Only the files that differ or are missing, through the staging area | Everything `compare` shows, then the stages below, with a private operation record |

**Connecting stops the app.** `compare` and `apply` stop the running assistant app and leave the Stick at the MicroPython REPL, like `stick_backup.py` (docs/stick-backup.md, "What connecting does"). Without `--restart`, the app stays stopped until the Stick restarts. The tool then prints the exact restart command.

## What is refused, before anything is written
**The backup itself,** checked offline before any port is opened, by `plan` as well:
* **Not complete:** a status other than `complete` (including `in-progress` from a killed backup), recorded failures, no finish time, or an unknown tool version.
* **Wrong firmware:** it was not taken from the supported firmware, or names no valid device id.
* **Damaged or altered:**
  * a stored file is missing, truncated or altered (size and SHA-256 against the manifest);
  * a file record isn't verified against the Stick, or is listed twice;
  * a file in `files/` isn't listed in the manifest;
  * the manifest isn't readable.
* **Unsafe paths:** `..`, empty or odd segments, anything outside `/flash/`, or a symbolic link anywhere: the backup folder itself, a folder inside it, or a file.
* **Secrets and the staging area:** any name starting with `device_key`, whether listed in the manifest or merely present, and any path inside `/flash/.stick-restore`.

**At the Stick (`compare` and `apply`):**
* **Wrong device:** a different device id than the backup's.
* **Firmware:** unidentified, or unsupported.
* **Conflicts:** a folder where a backup file belongs, a file blocking a needed folder, or a file that can't be read.
* **Space:** not enough free space on the Stick for the staged copies, plus 8 KB per file and 64 KB. Space held by an earlier run's staged copies counts as free, because they are removed first.
* **Space for the record:** less than 1 MB free on the Mac.
* **Unexpected write set:** with `--expect-changes`, the files that would be written aren't exactly the ones named.

## What `apply` does, and what an interruption means at each step
Each step is written to the operation record **before** it starts, so even a killed run says where it stopped.

| Step | What it changes on the Stick | Interrupted here: is the app intact? | Safe to restart the Stick? |
|---|---|---|---|
| validate, identify, compare, space | nothing | yes | yes |
| revalidate: every source file's size and SHA-256 again, just before the first write | nothing | yes | yes |
| prepare: create `/flash/.stick-restore/<op>/stage/…`, and remove **staged copies** left by earlier runs | only the staging area | yes | yes |
| staging: copy each file into the staging area | only the staging area | yes | yes |
| verify-staged: SHA-256 and size of every staged copy, computed **on the Stick** | nothing | yes | yes |
| **activating:** for each file, move the current version (if any) to `/flash/.stick-restore/<op>/prev/…`, then move the staged copy into place | **the app's files** | **not necessarily:** old and new files may be mixed, and a file may be **missing** (moved to `prev/`, new copy not yet in place) | **no** |
| **verify-final:** SHA-256 and size of **every** file in the backup on the Stick | nothing | only if it passes | only if it passes |
| cleanup: remove `/flash/.stick-restore/<op>/` (the staged copies and the replaced previous versions), then `/flash/.stick-restore` itself if it is empty | only the staging area | yes, already verified | yes |

**Rerunning after an interruption.** Run the same `apply` command again.
* It compares by hash: files that already match are skipped, and missing or different ones are staged, verified and activated again.
* It creates a new operation, with its own record and its own staging folder. Earlier records are never rewritten.
* Leftovers from earlier runs:
  * **staged copies** (data from the Mac) are removed;
  * **previous versions** in an earlier operation's `prev/` are **kept and reported**, never deleted automatically, because they may be the only copy of what was on the Stick before.
  * They aren't used by the app. Remove them by hand only once you no longer need them.

**A killed run** (Mac crash, closed terminal) can't finish its record: it stays `in-progress`. Its `stage` and `safe_to_restart_app` fields say which row above applies. A run that fails normally ends with status `failed` (or `interrupted` after Ctrl-C) and the same two fields.

**Success** means status `complete`, printed as `RESTORE COMPLETE`. It is reported only after the final step has verified **every** file in the backup on the Stick by SHA-256 and size. A failure at any step is never reported as success: the exit code is non-zero and the record says `failed`.

## When it is safe to restart the Stick
* **After `RESTORE COMPLETE`:** yes. `--restart` restarts it.
* **After a failure before activation began:** yes. The app's files were not touched, so `--restart` restarts it.
* **After a failure, interruption or kill during activation or final verification:** **no**. `--restart` is ignored, and the tool says `Do NOT restart the Stick yet`. Rerun `apply` until it completes, then restart. If the Stick restarts anyway (power, battery), the app may not start. Rerunning `apply` over USB talks to MicroPython, not to the app, so it should still work; that has not been tested on hardware.

## Exact limits
* **Files only.** It restores the files under `/flash` that are in the backup. The **device key** is never written, restored or deleted; it isn't in any backup, and a backup containing one is refused. **NVS settings** (Wi-Fi credentials, `boot_option`) and the **firmware**, bootloader and partition table are never written.
* **Not an exact rollback.** Files on the Stick that aren't in the backup are **left in place, never deleted** (`compare` lists them). After a restore, the Stick has the backup's files *plus* any newer extra files.
* **Not atomic.** A multi-file restore is not atomic and is **not claimed to be boot-safe** at any intermediate point.
  * The activation order (other files first, then the app modules in deploy order, then `boot.py` and `main.py` last) only reduces risk.
  * No intermediate mix has been started on a Stick.
  * Each file is moved into place by rename, after its previous version was moved aside, so the tool never relies on "rename over an existing file".
* **Verified bytes, not a working app.** `complete` means every file in the backup matches it on the Stick. It doesn't prove the app starts, that the backup itself was a working state, or that a restart will succeed.
* **One device and one firmware.** Only onto the Stick the backup came from (same device id), and only on the supported firmware (docs/stick-backup.md).

## The operation record
`<data_dir>/stick-restores/stick-<device id>/<operation>/record.json`: a new folder per run (`0700`, file `0600`), outside git (a location git would track is refused).

**It contains:**
* status, current stage, `safe_to_restart_app`, and the stage history with times;
* the backup folder and its manifest's SHA-256, and the device identity (presence-only flags, as in the backup);
* the comparison (paths per category);
* per written file: action (replace or create), size, SHA-256, and whether it was staged, staged-verified, activated and finally verified;
* files left on the Stick, previous versions kept from earlier runs, failures, and these limits.

**It never contains file contents.** The tool prints names, sizes and states only.

## Verification so far
`tests/test_stick_restore.py` has 22 cases. The simulated Stick is writable for these cases only; its rename refuses to replace an existing file, and every write is logged. Backups are made with `stick_backup.py` against the same simulated Stick.

**Covered:**
* the offline plan, which opens no port and has no port option;
* an explicit port for `compare` and `apply`;
* a comparison that writes nothing;
* a complete restore: only differing files sent, all through staging, the final hashes equal to the original, the extra file kept, the key untouched, a private record;
* nothing written when everything already matches;
* refused backups: 11 incomplete, damaged or altered variants;
* unsafe paths and symbolic links (file, folder, the backup itself);
* a device key in a backup (listed, or present under another spelling);
* a wrong device, unsupported firmware, insufficient space, and a folder conflict, all before any write;
* interruptions during transfer (failed copy, disconnect, damaged staged copy) leaving the app's files untouched, with a rerun finishing;
* an interruption during activation (a module missing), refusing a restart, then a rerun finishing with the earlier previous version kept;
* a run killed during activation, recorded as not safe;
* a file that fails final verification, reported as a failure;
* `--expect-changes` refusing any other write set, and the empty staging folder removed only when empty.

**Breakage check:** eight deliberate breakages of the tool were each caught, plus three for `--expect-changes` and the cleanup of the empty folder:
* skipping final verification;
* treating activation as safe to restart;
* accepting an incomplete backup;
* not protecting the device key;
* writing straight to the target without staging;
* following symbolic links;
* rewriting matching files;
* not checking the device id.

**Not verified on hardware** (beyond the single-file test below):
* any multi-file restore, or replacing an app module;
* any interruption, disconnect or power loss during a restore, and recovery by rerunning;
* staged copies or kept previous versions left over from an earlier run;
* transfer speed and timeouts for large writes over real USB;
* whether the app starts after a restore;
* any restore onto another Mac, cable, Stick or firmware.

## Single-file hardware restore test (2026-11-19, the author's Stick, with the owner's authorization)
**What it was:** one restore of **one file the app never uses**. It is not a test of multi-file recovery, of replacing app files, or of interruption or power-loss safety.

**Setup:** tool commit `16ced1c`; mpremote 1.29.0; the Stick's port, the only candidate, named explicitly; supported firmware (UIFlow V2.4.8, MicroPython 1.27.0). Every command sent to the Stick went through a wrapper that logged its arguments (commands and the device-side snippets, never file contents). The log and each step's output are kept privately next to the operation record.

**Before any change, `/flash/README.md` was checked as unused:**
* **The app:** neither v0.11.1 (on the Stick) nor this branch refers to README anywhere in its Stick code or kit tools.
* **Boot:** the Stick's `boot.py` is byte-identical (SHA-256) to a copy of the vendor file kept by the maintainers. With `boot_option` 0, it opens only `main_ota_temp.py` and `main.py`, and calls UIFlow 2.4.8's public `startup()`, which only initializes the board and reads settings.
* **One gap:** `boot.py` also *imports* `m5sync` (not run for option 0), whose source isn't public. The edited README existed only while the app was stopped at the REPL, with no boot until it had been restored and verified.

**The app was stopped once**, from 12:59:03Z (backup) to the restart at 13:03:00Z, with no restart in between.

**Steps:**
1. **Fresh backup:** `stick_backup.py backup --expect-device <id>`. Complete: 33 files verified, the device key excluded. Checked independently afterwards (private modes, hashes, no key-named file, outside git). Nothing had changed since the backup taken at 11:48.
2. **Offline plan:** no command sent to the Stick.
3. **`compare`:** 4 read-only commands; 33 files already matched, with no extra files. Free space read as 572 KB (`statvfs` worked on the real `/flash`).
4. **`apply` with nothing to write:** 4 commands, all read-only. The audit found no copy to the Stick, no create, rename or remove, nothing opened for writing, no settings write and no reset. The record shows only `identify` and `compare`.
5. **The change:** one 95-character HTML comment line (97 bytes with newlines) appended to `/flash/README.md` with mpremote, outside the tool.
6. **`compare` again:** exactly `/flash/README.md` to be replaced, 32 files matching, nothing to create, no extras.
7. **`apply --expect-changes /flash/README.md`:** 10 commands in 18 s; complete. Its only writes:
   * creating `/flash/.stick-restore/<op>/stage`;
   * one copy into it, verified on the Stick;
   * activating `README.md` only: the edited version moved to `<op>/prev/`, then the staged copy moved into place (renames across folders on the real file system, never over an existing file);
   * after all 33 files verified, removing `<op>` and the then-empty `/flash/.stick-restore`.
8. **Independent check before restarting:** `README.md` was 757 bytes with the backup's SHA-256; the staging folder was absent; all 33 files matched.
9. **Restart:** the reset command, then a fresh authenticated report with a new boot about 12 s later. The port was released, and one server was running, not restarted.

**What this shows, once, on this Stick:** the restore's writing snippets work on its `/flash` for one non-app file. That covers creating folders, mpremote copying to the Stick, staged verification, renames across folders, recursive removal, `rmdir`, `statvfs`, the `--expect-changes` guard and cleanup.

**What it does not show:**
* multi-file recovery, or replacing app modules;
* that the app starts after its own files are restored;
* staging, activation or rerun recovery after an interruption, disconnect or power loss (simulated only);
* timing for large files;
* another Mac, Stick or firmware.
