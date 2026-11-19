# Stick identification and private file backup (`tools/stick_backup.py`)

**Status:**
* **Real hardware:** one identification and one backup ran successfully on the author's Stick (supported firmware, 2026-11-19; see "Verification so far").
* **Simulated only:** every failure path, other firmware, and a missing Wi-Fi setting or boot option (`tests/test_stick_backup.py`).

```sh
python3 tools/stick_backup.py ports                                      # lists /dev/cu.usbmodem* names; opens nothing
python3 tools/stick_backup.py identify --port /dev/cu.usbmodemXXXX       # add --json for machine-readable output
python3 tools/stick_backup.py backup   --port /dev/cu.usbmodemXXXX       # identify, then a verified copy of /flash minus the device key
#   --expect-device ID     refuse unless the Stick reports this device id (from an earlier identify)
#   --allow-unsupported    back up a Stick whose firmware is identified but not the supported version
#   --restart              restart the Stick at the end, so the assistant runs again
```

**The port is always yours to choose.** With no `--port`, or several candidates, the tool lists what it sees and stops without opening anything. It doesn't connect even when exactly one candidate exists.

## What connecting does to the Stick
Both `identify` and `backup` talk to the Stick through the Stick tool (`mpremote connect <port> resume ...`).
* **Every command starts with Ctrl-C.** That **stops the running assistant app** and leaves the Stick at the MicroPython REPL (the raw REPL while a command runs).
* **No reset by default.** `resume` skips MicroPython's soft reset, and the tool never resets the Stick unless you pass `--restart`.
* **Opening the port might reset it anyway.** Whether opening a USB serial port resets an ESP32-S3 depends on the USB stack. On the development unit it didn't, but that isn't a guarantee.
* **Until the Stick restarts, the assistant is off:** no listening, reminders or notifications. To restart it, use `--restart`, or run the command the tool prints:
  `<mpremote> connect <port> resume exec --no-follow "import machine; machine.reset()"`.
  Unplugging may not restart it, because the Stick has a battery. A physical button restart hasn't been documented or tested for this kit.
* **Nothing is written to `/flash` or to the settings area (NVS).** That is not the same as "no impact": the assistant is stopped.

## What `identify` reports
Values that can't be read are shown as **unknown**, never guessed.

| Item | Source on the Stick | Notes |
|---|---|---|
| Device id | SHA-256 of `machine.unique_id()`, first 12 hex characters | Names the backup folder. It is not a secret: the chip id is the MAC address, which can be recovered from it by trial, so don't publish it |
| Model, MicroPython version and build | `os.uname()` | |
| UIFlow firmware | `esp32.firmware_info()[3]` | The same call the app uses; anything not shaped like `V2.4.8` is unknown |
| Boot option | NVS `uiflow` / `boot_option` | `0` means this assistant starts by itself; "not set" if the key is missing |
| Wi-Fi saved | NVS `ssid0` / `pswd0` | **Presence only.** The device answers yes or no, and the values never leave the Stick |
| Device key | whether `/flash/device_key.txt` exists | Presence only |
| Assistant app | SHA-256 on the Stick of each app file, compared with this kit | "matches this kit", "differs (n of m identical)", "not installed" or unknown. `APP_VERSION` isn't used, because it hasn't changed since v0.9 |

### Supported firmware policy
* **Supported:** a **StickS3** running **UIFlow2 V2.4.8, MicroPython 1.27.0, build 2026-06-26**. This is the only combination the assistant was validated on, on one unit. The microphone fix in particular depends on this firmware's driver behavior.
* **Unsupported:** identified, but anything else (another UIFlow or MicroPython version, build or model). Reported with the reasons. Nothing here claims other versions work, or that they don't.
* **Unknown:** something couldn't be read, or the device didn't answer as MicroPython (another firmware, UIFlow1, factory demo). No backup is made.
* **What it means for backup:** a backup is made automatically only for a supported Stick. An unsupported one needs `--allow-unsupported`, and its manifest records that.

## What `backup` does
1. **Check the location.** It refuses to start if the backup folder would be inside the project and not ignored by git.
2. **Identify the Stick.** It refuses if the Stick is unknown, if it isn't the `--expect-device` id, or if it's unsupported without `--allow-unsupported`.
3. **List `/flash`.** That means names and sizes only. If the list is cut off (the end marker never arrived), it stops before copying.
4. **Check disk space.** It refuses unless there is free space for the listed files plus 20 % and 1 MB.
5. **Create a new folder,** `<data_dir>/stick-backups/stick-<device id>/<UTC time>-<random>/`. It is created exclusively, so an earlier backup is never reused or overwritten. Folders are `0700` and files `0600`.
6. **Copy each file except the device key.** It writes `manifest.json` with status `in-progress` first, then copies the files one by one. After a disconnect, the remaining files are listed as "not attempted".
7. **Verify every copy.** The SHA-256 and size are computed on the Stick and compared with the copy. A partial or damaged transfer counts as a failure.
8. **Re-identify the Stick.** A different device, or no answer, makes the backup incomplete.
9. **Finish the manifest.** `status` is `complete` only when every listed file was copied and verified and the same device answered at the end. Otherwise it is `incomplete`, with the reasons. **A manifest still saying `in-progress` means the run was killed: treat it as incomplete.**

**The manifest lists:** each file's path, size, local and on-device SHA-256, and whether it was verified. It also lists exclusions (path and reason only: the key's size and hash are not recorded), skipped names, failures, the identification, these limits and the connection note.

## Privacy
* **Everything is potentially sensitive:** the settings cache, the device configuration, and anything else on the Stick.
* **Where it lives:** the configured data folder (git-ignored by default), with private permissions. It is never printed, uploaded or committed by this tool, and the tool doesn't print file contents.
* **The device key is never copied and never hashed.** Wi-Fi values are never read out of the Stick.

## What the backup is not, and cannot restore
* **Not a firmware image.** Not the bootloader, partition table or the **NVS** settings area. Wi-Fi credentials, `boot_option` and other UIFlow settings live in NVS, so they aren't in the backup.
* **Not the device key.** It is excluded on purpose. After a restore, a new key would have to be provisioned and paired (`tools/stick_setup.py key`, then `verify`). Anything named like the key (`device_key*`, such as a partial copy) is excluded too.
* **Nothing outside `/flash`.**
* **Can't bring back a Stick whose firmware is damaged or replaced.**
* **Not proof that recovery works.** This tool never restores, deploys or flashes. Restoring files is a separate tool, `tools/stick_restore.py` (docs/stick-restore.md), tested **only against a simulated Stick**. No restore has been run on real hardware.

## Verification so far
`tests/test_stick_backup.py` covers 23 cases against the simulated Stick, with the isolation guard active:
* explicit and ambiguous ports;
* supported, unsupported and unknown firmware, and unreadable values;
* Wi-Fi presence without values;
* a complete private backup, and the device key's exclusion;
* never overwriting an earlier backup;
* an expected-device mismatch, and a device change mid-backup;
* a disconnect, a partial read, a failed copy, a truncated file list, insufficient disk space, and a killed run;
* restart only on request;
* a tracked backup location refused.

Five deliberate breakages of the tool were each caught: copying the key, marking failures complete, auto-picking a single port, sending a Wi-Fi value back, and skipping the hash comparison.

### One real-device run (2026-11-19, the author's Stick, with the owner's authorization)
Tool commit `d617ec1`; Stick tool mpremote 1.29.0; the Stick's port, the only candidate, named explicitly.

**Backup location:** `STICK_DATA_DIR=~/sticks3-private-data`, outside any git repository. It was not the worktree's `data/`: git ignores it, but removing the worktree would delete it.

**Before connecting:**
* the device-side code was reviewed;
* nothing held the port;
* the Stick was reporting normally.

**`identify`** (1.4 s):
* every field was read, and the firmware is **supported**: StickS3 (`M5STACK StickS3 with ESP32-S3-PICO-1`), MicroPython 1.27.0 (`v1.27.0-dirty on 2026-06-26`), UIFlow V2.4.8;
* boot option 0; Wi-Fi network name and password both saved (presence only); device key present;
* app: 7 of 11 kit files identical, with `device.py`, `notify.py` and `voice_send.py` different and `server_config.py` absent;
* the app result matched a per-file prediction made beforehand (the Stick runs v0.11.1, and this branch changes those files);
* the device id (kept in the private manifest, not here) also matches an id derived beforehand from the USB descriptor's serial number, read without opening the port.

**`backup --expect-device <that id> --restart`** (94 s for about 1 MB):
* **complete:** 33 files copied and 33 verified; `/flash/device_key.txt` excluded; no failures or skipped names.
* **Checked independently afterwards,** without opening or printing any file:
  * every folder is `0700` and every file `0600`;
  * no file named like the key exists;
  * for all 33 files, the local hash, the manifest hash and the Stick's hash agree, and the sizes match.

**Restart:** the Stick reported a new boot with fresh contact through the authenticated device link about 23 s after the backup ended. No process held the port afterwards.

**What that run verified:**
* the identify, list and hash snippets under real MicroPython;
* the presence check for saved Wi-Fi values, and the device key's exclusion;
* copy and verification over real USB;
* the restart.

**Not verified on hardware:**
* every failure path (disconnect, partial read, device change, insufficient space, an interrupted run);
* the "not set" and "not saved" answers, which depend on the assumed NVS not-found code;
* unsupported or unknown firmware;
* whether opening the port resets the Stick: not separately observed in this run;
* any other Mac, cable or Stick;
* any restore.
