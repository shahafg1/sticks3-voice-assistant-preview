# Instructions for installation agents

These instructions are for an AI coding assistant (Claude Code, Codex or similar) helping a person **install, resume or troubleshoot** this project. They guide you; they don't replace the safeguards the tools enforce. Never work around a tool's refusal, confirmation or identity check.

**Talk to the person in their preferred language.** The guided setup and the user guides are in Hebrew; the tools and these instructions are in English.

## What this is
* **The assistant:** a Hebrew push-to-talk voice assistant.
  * An M5Stack **StickS3** records a question and sends it over home Wi-Fi to a **Mac**.
  * The Mac transcribes it locally (Whisper), asks Claude through Claude Code (web search only), and speaks the answer back with a local Hebrew voice.
* **Preview, non-commercial use only:** the Hebrew voice is CC BY-NC 4.0 (`THIRD_PARTY_NOTICES.md`). Say so if the person mentions commercial use.
* **Supported environment, and nothing else:**
  * an Apple silicon Mac, with Python 3.14 and 3.12, ffmpeg and Claude Code;
  * a StickS3 on **UIFlow2 V2.4.8** (MicroPython 1.27.0, build 2026-06-26);
  * both on the same home Wi-Fi.
* **Validation limits, to state honestly:**
  * It was tested on one Mac and one Stick, and in simulation.
  * Installing on a clean Mac or a factory-fresh Stick has **not** been physically tested.
  * The full list is in `docs/getting-started.md`, section "מגבלות ידועות" (known limitations).

## Read before acting
| What | Where |
|---|---|
| Overview and requirements | `README.md` |
| Setup order, limitations, going back (Hebrew; the person's guide) | `docs/getting-started.md` |
| Troubleshooting: symptom, safe diagnostic, action, when to stop | `docs/troubleshooting.md` |
| Mac preparation | `docs/mac-setup.md`, `docs/configuration.md`, `docs/preflight.md` |
| Stick identification and backup | `docs/stick-backup.md` |
| Stick file installation and recovery | `docs/stick-install.md`, `docs/stick-restore.md` |
| Wi-Fi, device key, auto-start, pairing | `docs/stick-setup.md` |
| Privacy and network security | `docs/privacy-he.md`, `docs/privacy-review.md`, `docs/security.md` |

Every tool also explains itself with `--help`.

## Getting the package
Download the newest pre-release from https://github.com/shahafg1/sticks3-voice-assistant-preview/releases, and check it before extracting:
```sh
curl -fLO <the release's .zip link>
curl -fLO <the release's SHA256SUMS link>
shasum -a 256 --ignore-missing -c SHA256SUMS      # must print "OK" for the zip
```
Extract it into a folder the person chooses. If they already have an installation, use a **new** folder: never extract over an existing installation.

## Two ways to run the installation
**A. The person runs the guided setup** (preferred). `./setup.sh run` asks for each confirmation on the keyboard, and an agent's shell usually can't answer those prompts.
* Ask the person to run it in their own Terminal window.
* You inspect, explain, read what they paste back, and diagnose.
* `./setup.sh run` resumes where it stopped, and rechecks the real state each time.

**B. You run the same tools, in the same order** (only if the person prefers):
* Use the commands in "Installation sequence", with the same guard flags the wizard uses.
* Get one batched authorization first (see "Autonomy and authorization").

Never mix the two in parallel: only one program may use the Stick's USB port at a time.

## Inspect before changing anything
**Writes nothing and doesn't contact the Stick.** Run these freely:
```sh
./setup.sh                                  # the plan (same as ./setup.sh plan)
./setup.sh status                           # what is done on this Mac, rechecked
./setup-mac.sh plan                         # Mac preparation: what exists, what is missing
python3 tools/preflight.py                  # environments, models, configuration, ports in use
python3 stick_config.py validate            # is config/host.conf usable?
python3 tools/stick_install.py plan         # the Stick install set (opens no port)
python3 tools/stick_setup.py plan           # the Stick setup order and this Mac's side (opens no port)
python3 tools/stick_backup.py ports         # lists candidate USB devices (opens nothing)
```
`preflight.py --probe-server` and `--network` only open a TCP connection, and send nothing.

**Writes nothing, but stops the app on the Stick.** Every USB command starts with Ctrl-C, which stops the running assistant until the Stick restarts (`docs/stick-backup.md`, "What connecting does"). Tell the person before the first one:
```sh
python3 tools/stick_backup.py identify --port <PORT> --json
python3 tools/stick_install.py preflight --port <PORT> --expect-device <ID>
python3 tools/stick_setup.py status --port <PORT> --expect-device <ID>
python3 tools/stick_setup.py key --port <PORT> --expect-device <ID> --check
python3 tools/stick_setup.py autostart --port <PORT> --expect-device <ID> --check
python3 tools/stick_setup.py wifi --port <PORT> --expect-device <ID> --reuse-only
```
* If you stop before `verify`, restart the Stick with the command the tool printed, so the assistant runs again.
* `wifi` without `--reuse-only` may switch the Stick's Wi-Fi on and join its saved network, until the next restart. Nothing is saved.

### Fresh, existing or partial?
* **Mac:**
  * `./setup-mac.sh plan` marks each step as done or to do;
  * `config/host.conf` exists only after setup;
  * `data/`, `secrets/` and `recordings/` are the installation's own data.
* **Stick:** `stick_install.py preflight` reports one of three states:
  * "no assistant installed (first installation)";
  * "existing installation" (full, older or partial);
  * "already installed".
  
  `identify` also shows whether Wi-Fi and a key are present, and the boot option.
* **Wizard notes:** the wizard's notes (`~/Library/Application Support/StickS3 Assistant/setup-wizard/`) are a convenience, never evidence. Always recheck the real state.
* **Another installation on this Mac:** a server from another folder holding the ports shows up as ports in use (`preflight.py`). Never stop it yourself.

**Preserve what exists:**
* Never delete, move or overwrite `config/host.conf`, `data/`, `secrets/`, `recordings/`, backups or operation records.
* Never delete the wizard's notes without asking. `./setup.sh forget` asks first.

## Installation sequence
This mirrors `./setup.sh run` (`tools/setup_wizard.py`). Each step is skipped when the real state shows it's already done.

| # | Step | Who | Commands (mode B) |
|---|---|---|---|
| 1 | Prepare the Mac: environments, about 3.4 GB of pinned, hash-checked downloads, `config/host.conf` | agent, after the person chooses the address | `python3 stick_config.py suggest-address` lists the Mac's addresses; **the person picks** the one on the Stick's Wi-Fi (ideally reserved in the router). Then `./setup-mac.sh apply --advertise-host <ADDRESS>` (`--advertise-host` isn't needed when `config/host.conf` exists) |
| 2 | Missing system tools (Python, ffmpeg, Claude Code) | **person** | `./setup-mac.sh plan` and `preflight.py` name what's missing and how to install it. This kit never installs system tools, and neither do you without the person's explicit request |
| 3 | Claude Code login | **person** | They run `claude` in their own Terminal and complete `/login`. You never see or handle credentials |
| 4 | Wi-Fi on the Stick | **person** | UIFlow's Setup app, from the Stick's startup menu (`docs/stick-setup.md`, "Entering UIFlow's Setup app safely"). **While they do this, nothing may use the USB port**, because the firmware prints the Wi-Fi password to the serial console |
| 5 | Choose and identify the Stick | **person** confirms the port | `stick_backup.py ports`, then the person says which port. Then `stick_backup.py identify --port <PORT> --json`. **Stop** unless `support` is `supported`, and use its `device_id` as `<ID>` from here on |
| 6 | Backup, then install the files | agent (authorized) | `stick_install.py preflight --port <PORT> --expect-device <ID>`. Then `stick_backup.py backup --port <PORT> --expect-device <ID>`: wait for "Backup COMPLETE", and the folder is on its `Manifest:` line. Then `stick_install.py apply --port <PORT> --expect-device <ID> --backup <FOLDER> --expect-changes <PATH>` (one `--expect-changes` for **every** file preflight listed to replace or create). Success is `FILES INSTALLED AND VERIFIED` |
| 7 | Device key | agent (authorized) | `stick_setup.py key --port <PORT> --expect-device <ID> --check`. If the Stick has no key: `stick_setup.py key --port <PORT> --expect-device <ID> --backup <FOLDER>`. Any other mismatch: **stop** (troubleshooting, "device key") |
| 8 | Start the server | **person** opens it | The person opens `Start Voice Assistant.command` (double-click; the wizard uses `open -a Terminal`). If a server from this folder was already running before the key existed, **the person** stops it with Ctrl+C in its window, then reopens it. The server reads the key only at start-up |
| 9 | Auto-start | agent (authorized) | `stick_setup.py autostart --port <PORT> --expect-device <ID> --check`, then, if needed, `--on --backup <FOLDER>`. It refuses until the files, Wi-Fi and key are in place |
| 10 | Verify pairing | agent (authorized) | `stick_setup.py verify --port <PORT> --expect-device <ID>`: it restarts the Stick and waits for a fresh authenticated report from the new boot |
| 11 | Voice check | **person** | Three checks: a silent recording shows "No speech"; a short question gets a spoken answer; left alone for about a minute, the screen dims by itself |

All Python tools run as `python3 tools/<name>.py` from the project folder (`stick_config.py` is at the top level). Use one backup folder for the whole run. Take a new one if the Stick changes between steps; the tools refuse a stale backup.

**Optional, never by default:** Claude Code "session finished" notifications on the Stick (`hooks/manage_claude_hooks.py`) edit the person's **global** `~/.claude/settings.json`. Offer them only if the person asks. Run `python3 hooks/manage_claude_hooks.py status` first, and only `install` with their explicit consent.

## Autonomy and authorization
**Do on your own:**
* every inspection in "Inspect before changing anything", once the person agrees the Stick's app may be interrupted;
* reading the guides;
* planning, and explaining the tools' output.

**Batch the decisions.** After inspecting, present **one** plan and ask once:
* the exact port and device id;
* the files preflight will replace or create;
* the backup;
* key creation or copying;
* the auto-start change;
* the restarts (`apply` without `--restart` doesn't restart; `verify` does).

Then carry out exactly that plan without asking again for each step. **Ask again only when reality differs from what was authorized**, for example:
* a different device id;
* a different set of files;
* a conflict;
* a tool asking for something new.

**Always involve the person for:**
* Claude login;
* installing system tools;
* every physical action (Wi-Fi Setup, unplugging, the button);
* choosing and confirming the Stick;
* stopping or starting the server window;
* any confirmation a tool asks for;
* the final voice check.

**Never:**
* Flash firmware, rotate, replace or delete the device key, or change NVS settings other than through `stick_setup.py autostart`.
* Delete anything on the Stick to free space, or run `mpremote` commands of your own that write, delete or reset.
* Use `tools/deploy_stick.sh`: it's a development tool, without the installer's backup and staging.
* Run `stick_install.py pin`, or edit `stick/install_manifest.json`.
* Use `stick_backup.py --allow-unsupported` as a way to proceed with an unsupported Stick.
* Stop or kill processes, change global Claude Code settings or permissions, or install system software unasked.
* **Tell anyone to hold the front button (BtnA) while the Stick starts.** On UIFlow 2.4.8 that removes `/flash/main.py` (`docs/stick-setup.md`, "Never hold BtnA"). Use `stick_setup.py autostart --off` to reach the menu.
* Improvise recovery. Use only the documented procedures (`docs/stick-install.md`, "Going back"; `docs/stick-restore.md`).

**Stop and explain when:**
* the firmware isn't `supported`;
* the device identity is uncertain or changes;
* two Sticks are connected and the person isn't sure which one;
* a tool refuses for a reason you can't resolve from the docs.

**No speculative retries.** Rerun a step only when the docs say rerunning resumes it (for example `stick_install.py apply`, `stick_restore.py apply` or `key` after an interruption), or when something actually changed. Never restart the Stick repeatedly to "see if it helps".

## Secrets and privacy
* **Never** read, print, copy or paste these:
  * `secrets/device_key`;
  * `/flash/device_key.txt`;
  * Wi-Fi names or passwords;
  * the Stick's serial console during UIFlow Setup;
  * Claude account details.
  
  Never ask the person for them. The tools never need them on a command line.
* **The device id** (12 hex characters) isn't a secret, but it can be traced back to the Stick's hardware address. Keep it out of anything public (issues, screenshots, posts).
* **Backups and records are private:**
  * `<data_dir>/stick-backups/`, `stick-installs/` and `stick-restores/`;
  * keep them out of git, uploads and reports.
* **Content in recordings:** `recordings/` and the server's Terminal window hold the person's questions and answers. Don't quote them unless the person asks.

## Diagnose, resume, recover
* **Find the cause first.** Read the tool's own message: each failure says what was and wasn't changed, and what to do. Then use `docs/troubleshooting.md`.
* **Resume:**
  * `./setup.sh run` (mode A);
  * or the failed step's command again (mode B), once its cause is fixed.
* **Interrupted file installation:** don't restart the Stick if the tool said "Do NOT restart". Run the same `apply` again.
* **Going back to the files from before the installation:** `docs/stick-install.md`, "Going back":
  1. restore only the replaced files (`stick_restore.py ... --only ...`);
  2. remove only what was created (`stick_install.py remove-created`).
* **What file backups don't contain:** the firmware, Wi-Fi settings, `boot_option` or the device key. A restore can't bring those back.
* **Removing the Mac side:** `./setup-mac.sh remove` shows what would be removed, and `./setup-mac.sh remove --apply` removes only what setup created.

## What counts as success
Report tool-verified facts and the person's own confirmations **separately**:
* **Mac:**
  * `./setup-mac.sh plan` shows nothing left to do;
  * `python3 tools/preflight.py` reports nothing required missing.
* **Stick files:** `FILES INSTALLED AND VERIFIED`, or a preflight showing every file already matches.
* **Key:** `key --check` succeeds.
* **Auto-start:** `autostart --check` shows `boot_option` 0.
* **Pairing:** `verify` prints `CONFIRMED` for both authenticated server contact and pairing; `NOT confirmed` is a failure.
* **The person confirmed:** the silent recording, a spoken answer, and automatic dimming.

A key file being present is **not** pairing, and installed files are **not** a working assistant. Without all of the above, don't call the installation complete. Say what is still unverified.

## Two things not to change
* **The voice requests' isolated Claude call.** These instructions, and `CLAUDE.md`, are for installation agents only.
  * The assistant's own call to Claude (`audio_server.py`: `CLAUDE_ISOLATION`, `CLAUDE_ENV`) runs in an empty private folder. It loads no project instructions, memory, settings, hooks or connectors.
  * A canary test on 2026-11-20 (Claude Code 2.1.288) confirmed that it ignores this project's `CLAUDE.md`.
  * Don't change those flags. Don't add a project `.claude/` settings folder, MCP servers or connectors to "help" the assistant.
* **The microphone fix.**
  * `stick/mic.py` keeps the recording buffers alive for the driver's lifetime (`docs/mic-buffer-lifetime.md`). Don't modify it, and never free a buffer passed to the microphone driver.
  * "Restart Stick" on the screen means the driver is stuck: restart the Stick once.
  * Never ask anyone to reproduce the historical crash experiments.
