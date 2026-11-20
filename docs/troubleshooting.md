# Troubleshooting

Each problem is listed as **symptom → safe diagnostic → documented action → when to stop or recover**. For people and installation agents alike (agents: also read `AGENTS.md`).

**Checks are labelled by their impact:**
* **[writes nothing]:** reads the Mac only, or opens a short TCP connection that sends nothing. Doesn't touch the Stick.
* **[stops the app]:** connects to the Stick over USB. It writes nothing, but it stops the running assistant until the Stick restarts (`docs/stick-backup.md`, "What connecting does"). Restart it afterwards with the command the tool prints, or let `stick_setup.py verify` do it.

**First, whatever the problem:**
* Read the tool's own message. Every tool says what it did and didn't change.
* Use `./setup.sh status` and `python3 tools/preflight.py` **[writes nothing]** for a quick overall picture.

**Never:**
* flash firmware;
* delete files on the Stick to make room;
* replace the device key;
* hold the front button (BtnA) while the Stick starts: on UIFlow 2.4.8 that removes `/flash/main.py` (`docs/stick-setup.md`).

**A file backup holds only the files under `/flash`, and not the device key.** It does **not** hold the firmware, the Wi-Fi settings or `boot_option`.

---

## Missing prerequisites
* **Symptom:**
  * `./setup.sh` or `./setup-mac.sh` says Python 3.9+ wasn't found;
  * or `./setup-mac.sh plan` lists `MISSING` items (Python 3.14 or 3.12, ffmpeg, Claude Code).
* **Diagnose:** `./setup-mac.sh plan` and `python3 tools/preflight.py` **[writes nothing]**. Each missing item comes with an official install link.
* **Action:** the person installs what is missing (this kit never installs system tools), then reruns `./setup.sh run`.
* **Stop:** if the Mac isn't Apple silicon. It isn't supported (`docs/mac-setup.md`).

## Claude Code not logged in
* **Symptoms:**
  * The Stick shows an error after "thinking".
  * The server's Terminal window prints `Claude error:` followed by Claude Code's own message.
  * The job ends as "Claude failed" or "Claude timed out".
* **Diagnose:** the person runs `claude` in their own Terminal window and sees whether it asks them to log in. Preflight only checks that the CLI exists; it never runs it.
* **Action:** the person completes `/login` themselves. Nothing in this kit handles credentials. The next question uses the new login, without a server restart.
* **Stop:** if the account can't use Claude Code (plan or organisation limits). That's outside this kit.

## Missing or invalid configuration
* **Symptoms:**
  * The server window or a tool says the configuration is missing or invalid, and names `advertise_host`.
  * The Stick shows **"No server"** / **"Setup needed"**: its own `/flash/device_config.json` or `/flash/server_config.py` is missing or invalid.
* **Diagnose:**
  * `python3 stick_config.py validate` and `python3 tools/preflight.py` **[writes nothing]** report every problem at once.
  * For the Stick's side, `python3 tools/stick_install.py preflight --port <PORT> --expect-device <ID>` **[stops the app]** shows whether those two files are missing or differ.
* **Action:**
  * On a new installation, `./setup-mac.sh apply --advertise-host <ADDRESS>` writes `config/host.conf` once (`docs/configuration.md`). `python3 stick_config.py suggest-address` lists the Mac's addresses.
  * If the Stick's files are missing or out of date, rerun the file installation (`docs/stick-install.md`). It installs the Stick's configuration with the app.
* **Stop:** never overwrite an existing `config/host.conf` without the person's decision; edit only the value that is wrong. If the Mac's address keeps changing, the person should reserve one in the router.

## USB port unavailable, or the wrong device
* **Symptom:** a Stick tool refuses with one of these:
  * "Name the Stick's port explicitly";
  * "does not look like a Stick port";
  * "is not connected now".
  
  Or it can't open the port.
* **Diagnose:** `python3 tools/stick_backup.py ports` **[writes nothing]** lists the candidates.
* **Action:**
  * The person checks the cable (it must carry data, not only charge), and closes any other program using the port: another terminal, a serial monitor, or the wizard in another window.
  * Then they choose the port explicitly. Ports are **never** chosen automatically.
* **Stop:**
  * if two Sticks are connected and it isn't certain which is which;
  * or if `--expect-device` refuses: that is a **different Stick** from the one backed up or identified earlier. Don't use another backup or device id to "make it fit".

## Unsupported or unknown firmware
* **Symptom:** `identify` reports `unsupported` or `unknown`. The wizard says the firmware isn't supported.
* **Diagnose:** `python3 tools/stick_backup.py identify --port <PORT> --json` **[stops the app]** shows the model, UIFlow, MicroPython and build, with the reasons.
* **Action:** none in this kit. Only a StickS3 on **UIFlow2 V2.4.8** (MicroPython 1.27.0, build 2026-06-26) is supported (`docs/stick-backup.md`, "Supported firmware policy"). The microphone fix depends on that firmware's driver.
* **Stop:** always. This kit never flashes firmware and has no firmware guide. Changing firmware is the person's own decision, with M5Stack's official tools, outside this project.

## Wi-Fi not saved, or the Stick can't reach the server
* **Symptoms:**
  * `identify` or `status` shows no saved Wi-Fi.
  * `verify` says "NOT confirmed".
  * The Stick shows **"No Wi-Fi"**, **"Server lost"** or **"Timed out"**.
* **Diagnose:**
  * `python3 tools/stick_setup.py wifi --port <PORT> --expect-device <ID> --reuse-only` **[stops the app]** reports four levels: credentials saved, network connected, server port reachable, authenticated contact (`docs/stick-setup.md`, "Wi-Fi: on the Stick only").
    * Without `--reuse-only`, it may switch Wi-Fi on and join the saved network until the next restart. Nothing is saved.
  * `python3 tools/preflight.py --probe-server` **[writes nothing]** checks that the server is listening, and whether `advertise_host` is one of this Mac's current addresses.
* **Action:**
  * **Wi-Fi not saved:** the person uses UIFlow's Setup app (`docs/stick-setup.md`, "Entering UIFlow's Setup app safely"), with **no** tool connected to USB, because the firmware prints the password to the serial console. On an installed Stick, reach the menu with `stick_setup.py autostart --off --backup <FOLDER>` and a restart. Never use the button.
  * **Wrong address:** the Mac's address changed. Fix `advertise_host` in `config/host.conf`, then reinstall the Stick's configuration with the file installation.
  * **Port not reachable:** start the server (`Start Voice Assistant.command`). Accept macOS's firewall prompt for incoming connections, if one appears.
* **Stop:** the Mac and the Stick must be on the same home network. Never expose the server to the internet (`docs/security.md`).

## Device key missing or mismatched
* **Symptom:**
  * The Stick shows **"No device key"** when the button is pressed;
  * or the server window says `device key NOT provisioned` at start-up;
  * or `key --check` fails.
* **Diagnose:**
  * `python3 tools/stick_setup.py key --port <PORT> --expect-device <ID> --check` **[stops the app]** compares the keys with a random salt. It never shows them.
  * `python3 tools/stick_setup.py status --port <PORT> --expect-device <ID>` **[stops the app]**.
* **Action**, by the table in `docs/stick-setup.md` ("The device key"):
  * **No key on the Stick:** `stick_setup.py key --port <PORT> --expect-device <ID> --backup <FOLDER>`.
  * **This Mac's key is new:** the server reads the key only at start-up, so the person restarts the server (Ctrl+C in its window, then reopen it).
  * **Interrupted copy:** run `key` again. It reuses the same Mac key and replaces the partial copy.
* **Stop:**
  * if the Stick holds a **different** key, or holds one while this installation has none;
  * then check that `config/host.conf` points at the secrets folder the server uses.
  
  Rotation isn't implemented. Never delete or overwrite a key to "fix" it.

## Authentication failures
* **Symptom:**
  * The Stick shows **"Key rejected"**;
  * or the server window prints `[device] authentication failed from <address>`;
  * or, after many failures, the Stick shows **"Server busy"** on every request (a one-minute lockout).
* **Diagnose:** `key --check` **[stops the app]**. Confirm that the running server belongs to the same project folder as `config/host.conf` and its secrets folder.
* **Action:**
  * If the keys match, restart the server so it loads the current key.
  * Wait a minute after a lockout.
* **Stop:** if failures come from an address that isn't the person's Stick. That's another device on the network: don't share the key.

## The server's port is busy
* **Symptom:**
  * `preflight.py` lists the configured ports as in use;
  * or the wizard says another process holds them;
  * or the launcher says the assistant is already running.
* **Diagnose:** `python3 tools/preflight.py` **[writes nothing]**.
* **Action:**
  * **Another program holds the port:** change the ports in `config/host.conf` (`docs/configuration.md`).
  * **This project's server is already running:** use it, or stop it with Ctrl+C in its window.
  * **An assistant from another folder holds the port:** the person stops it in its own window.
* **Stop:** tools and agents never stop or kill processes. Exactly one server should run.

## Interrupted installation or restore
* **Symptom:** `stick_install.py apply` or `stick_restore.py apply` stopped part-way (cable, power, an error). It may say **"Do NOT restart the Stick yet"**.
* **Diagnose:** the tool's last message names the stage, and says whether any app file changed. `stick_install.py preflight` **[stops the app]** shows each file's state.
* **Action:**
  * Don't restart the Stick while the tool says not to.
  * Fix the cause and run the **same** `apply` again. It skips matching files and finishes the rest (`docs/stick-install.md`, "Writing, interruption and resuming").
  * With the wizard, `./setup.sh run` resumes.
* **Recover**, only if the person wants to go back to the files from before the installation: `docs/stick-install.md`, "Going back":
  1. restore only the replaced files (`stick_restore.py apply <BACKUP> --port <PORT> --only <PATH> …`);
  2. then remove only the created files (`stick_install.py remove-created <RECORD> --port <PORT> --expect-remove <PATH> …`).
* **Stop:** if a tool refuses a backup as stale, foreign or damaged. Take a fresh backup of the current state instead of forcing an old one.

## Not enough space on the Stick
* **Symptom:** `stick_install.py preflight` or `apply` reports that the files don't fit. Nothing is written.
* **Diagnose:** `stick_install.py preflight --port <PORT> --expect-device <ID>` **[stops the app]** shows the space needed and free. Previous versions kept from an interrupted run (`/flash/.stick-restore/`) are reported and take space.
* **Action:** none automatic. This kit never deletes files to make room.
* **Stop:** report the numbers to the person, and let them decide what of their own files to remove.

## Recording, speech or playback problems
Server-side failures appear on the Stick as **"Error"** followed by the reason.

| What the Stick shows | Meaning | Action |
|---|---|---|
| **"No speech"** / "Try again" | The server found no speech in the recording. Normal for a silent press | Speak closer, while pressing |
| **"Microphone failed"** | The microphone didn't start | Restart the Stick once. If it persists, stop: hardware or firmware |
| **"Restart Stick"** | The microphone driver is stuck; the fix detects this instead of crashing | Restart the Stick once (`docs/mic-buffer-lifetime.md`). Don't experiment with the microphone code |
| **"Server busy"** | Rate limit (6 per minute, burst of 3), 2 recordings already waiting, or a key lockout | Wait a minute |
| "Transcription failed" | Speech recognition failed on the Mac | Read the server window. Run `python3 tools/preflight.py` **[writes nothing]** for the server environment and ffmpeg |
| "Voice failed" | Hebrew speech generation failed on the Mac | Read the server window. Run `python3 tools/preflight.py --hashes` **[writes nothing]** to check the voice model files |
| "Claude failed" / "Claude timed out" | Claude Code failed or took longer than 120 s | See "Claude Code not logged in"; check the internet connection |
| "Voice reply unavailable" / "Voice file incomplete" | The reply download failed | Ask again. If it repeats, check Wi-Fi with `stick_setup.py wifi --reuse-only` |
| "Server restarted" | The server restarted while the question was being answered | Ask again |

**Diagnose without the Stick.** Watch the server's Terminal window while the person presses the button. Each stage (speech detection, Whisper, Claude, voice) prints there. The last recording, transcript and reply are in `recordings/`; they're private, so don't share them.

**Stop and report** rather than retrying, if the same error repeats after one restart of the Stick and of the server. Never ask anyone to reproduce the historical microphone crash: its evidence and fix are recorded in `docs/mic-buffer-lifetime.md`.

## The Stick doesn't start the assistant by itself
* **Symptom:** after power-on, UIFlow's startup menu appears instead of the assistant.
* **Diagnose:** `python3 tools/stick_setup.py autostart --port <PORT> --expect-device <ID> --check` **[stops the app]**. Also `stick_install.py preflight`, which reports a missing `/flash/main.py`.
* **Action:**
  * **`main.py` missing** (for example after BtnA was held at start-up): `stick_install.py apply --port <PORT> --expect-device <ID> --expect-changes /flash/main.py`.
  * Then `stick_setup.py autostart --port <PORT> --expect-device <ID> --on --backup <FOLDER>`.
  * Then `stick_setup.py verify --port <PORT> --expect-device <ID>`.
  
  Source: `docs/stick-setup.md`, "Never hold BtnA".
* **Stop:** if `autostart --on` refuses. It needs the files, Wi-Fi and the key in place first; fix what it names.
