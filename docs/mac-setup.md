# Mac host setup

`setup-mac.sh` prepares **the Mac side** of the voice assistant on an Apple silicon Mac: the three Python environments, the model files, and `config/host.conf`.
It does **not** make the assistant ready. Claude login, the network, and the Stick are separate steps that it lists at the end and never performs.

```
./setup-mac.sh plan                                  what would happen (read-only; this is also what you get with no argument)
./setup-mac.sh apply --advertise-host <address>      do it (the address is the Mac's address on your Wi-Fi; there is no default)
./setup-mac.sh apply                                 run it again: continues after an interruption, verifies instead of repeating
./setup-mac.sh remove                                show what removal would delete (read-only)
./setup-mac.sh remove --apply                        delete exactly what setup created
```
`python3 stick_config.py suggest-address` lists this Mac's addresses (it chooses nothing). Run from a terminal, `apply` without `--advertise-host` shows the list and asks you to type the number or the address; pressing Enter alone stops.

## Before you start (what is needed to run the setup itself)
Only **Python 3.9 or newer** with its standard library. Nothing else is needed to *start* (`plan` works without any environment). A new Mac has `/usr/bin/python3` only as a stub: running it opens
an "install the command line developer tools" window. `setup-mac.sh` therefore never runs that stub unless the tools are already installed (`xcode-select -p` succeeds); if no Python 3.9+ is found it says so and stops, changing nothing:
Homebrew (`brew install python@3.14`), the macOS installer from python.org, or `xcode-select --install`.
Because the setup also builds the environments, the **exact versions the project was tested with** must be installed: Python **3.14** (server, Stick tools) and **3.12** (Hebrew speech synthesis). Another version is refused unless you pass `--allow-untested-python`, and is then labelled untested everywhere.

## What it automates, and what stays yours
| Automated by `apply` | Still yours (listed in the report, never done by the tool) |
|---|---|
| Creates `voice-env`, `tts-env312`, `tools-env` in the project folder with pinned, hash-checked packages and proves they load | Installing Python, ffmpeg and Claude Code (guided below), Homebrew, Xcode tools |
| Downloads the Hebrew voice files and the Whisper model at **pinned revisions**, verifies size and SHA-256, and activates only complete files | **Claude login** and the optional Gmail / Calendar connectors (nothing is read or run) |
| Writes `config/host.conf` once, from the address you chose; never overwrites it | Wi-Fi, macOS firewall prompts, a stable Mac address (network) |
| Records what it created, resumes after interruption, and can remove exactly that | **The Stick**: firmware/UIFlow, Wi-Fi on the Stick, its code and its key (`tools/deploy_stick.sh`, README) |
| Ends with a report that separates host preparation from the rest | Starting the assistant; the licence review |

It never uses `sudo`, never installs system tools, never edits shell profiles, Homebrew, global Python packages or shared caches (pip runs with `--no-cache-dir`), never starts the server, never runs Claude, never reads account data, never installs hooks,
never changes sleep or firewall settings and never opens a serial connection. The report says **"THE ASSISTANT IS NOT READY YET"**; host preparation can be "complete" while that is still true.

## Prerequisite guidance (checked against the official pages on 2026-11-18)
| Need | Official instruction | What was checked |
|---|---|---|
| Claude Code | https://code.claude.com/docs/en/setup: native installer `curl -fsSL https://claude.ai/install.sh \| bash`, or `brew install --cask claude-code` | macOS 13+ required; **Node.js is not required**; logging in is a separate step. (The tool prints these lines; it does not run them: they download and run code, which is your decision.) |
| Homebrew (optional) | https://brew.sh: its installer explains what it will do and asks first | It may change your shell profile; the setup does not. `brew install` may also update Homebrew itself first (Homebrew's normal behaviour). |
| ffmpeg | `brew install ffmpeg` (https://formulae.brew.sh/formula/ffmpeg, stable 9.0.2, Apple-silicon bottles) | the setup only looks for it |
| Python 3.14, 3.12 | `brew install python@3.14 python@3.12` (both formulae exist, bottles for Apple silicon) or https://www.python.org/downloads/macos/ | the setup only looks for them |

## Reproducibility, honestly
* `requirements/*.lock` pin every package **by SHA-256** (`pip install --require-hashes --no-deps`); they are generated from the same pins as `requirements/*.txt` by a developer tool (not part of the preview package). A lock accepts **any published file of the pinned version** (the Mac model and macOS version vary), so it fixes the *content* of each package, not one particular file.
* Hashed: every PyPI package; the phonikud source (GitHub's archive of the pinned commit, SHA-256 recorded; the archive was identical across two downloads, but GitHub does not promise byte-stable archives, so a change fails closed); the TTS and Whisper model files (size + SHA-256 from `requirements/models.json`, pinned to commit revisions of the Hugging Face repositories).
* **Not covered (gaps):** (1) packages built from source (`docopt`, `phonikud`) fetch their *build tools* (setuptools and similar) through pip's isolated build, which is not hash-checked; (2) the Python interpreters themselves and the `pip` that ships inside each new environment come from your Python install, not from a pinned artifact; (3) Homebrew packages (ffmpeg, Python) and Claude Code are whatever their installers deliver; (4) the Hugging Face pins are commits, but the SHA-256 values were recorded from the development machine (Whisper) and the repository metadata (voice files): a real download is what proves them, and one is recorded below; (5) the locks target Python 3.14 / 3.12 on macOS; other versions are untested.
* Whisper: previously the code named the model but not a revision, so each machine got whatever was current. The server now uses the pinned local copy (`whisper-model/`, key `whisper_dir`) when setup made one and falls back to the Hugging Face cache otherwise, so existing installations behave as before.
* Model licences are visible in every plan and report: the Hebrew voice files are CC-BY-NC-4.0 (non-commercial), and the other models' terms are listed in THIRD_PARTY_NOTICES.md. Nothing here claims commercial readiness.

## How it stays safe
* **Ownership.** Every environment gets a marker file (`.stick-setup-partial.json` while being built, `.stick-setup-owned.json` when verified) carrying this installation's random id, and `.stick-setup/state.json` records what was created (paths, hashes; no secrets). A folder with no matching marker is **never modified or deleted**: existing `voice-env`, model files and configuration are kept and reported ("kept", not "ours").
* **Resume verifies real state.** Notes are never trusted on their own. An environment counts as done only if its own Python runs, every locked package is installed at the locked version, **and** the smoke modules import in a fresh process. A half-built or damaged environment that setup created is rebuilt; one it did not create is left alone.
* **Downloads** go to a hidden `.part` file next to the target, continue from what was received (HTTP Range), are verified for size and SHA-256, and only then moved into place. A corrupted or truncated file is deleted and never activated.
* **Concurrency.** One setup at a time (`.stick-setup/lock`, published atomically with the owner's process id). A second attempt stops with the other process's id; the lock of a process that died is taken over. If a lock is wrongly considered live, the message says which file to delete.
* **Nothing is changed before the checks pass:** unsupported Mac, missing Python, conflicting files, an unusable address or too little disk (the sum of what is still to download) stop `apply` before the first change.
* **Removal** deletes only: environments at their *currently configured* location that carry a matching marker and note, model files that still have the downloaded hash and whose names are in the pinned manifest, folders it created (only if empty), and its own notes. It never deletes by walking an arbitrary path, refuses links, the home folder, the project folder and their parents even if the notes were edited, and keeps `config/host.conf`, `data/`, `secrets/`, `recordings/`, backups, caches, and every system tool.

## Verification
**Automated, isolated (`tests/test_setup_mac.py`, 16 cases).** *Real:* virtual environments created with `python -m venv`, packages installed by `pip --require-hashes` from a tiny local wheel, files downloaded over HTTP from a local server (resume, truncated, corrupted), real processes for the lock, the shell entry point, Apple's own Python 3.9 running the plan. *Simulated:* the package index and Hugging Face (local stand-ins), the tested Python versions (the interpreter running the test), and Rosetta/missing-tool conditions. Cases: fresh setup, repeated setup, interruption and resume (environment and download), stale completion notes, corrupted and truncated downloads, missing Python, missing ffmpeg/Claude, missing or unusable address, Intel/Rosetta, existing unowned environments/files/configuration, concurrent attempts, removal boundaries, forged notes, read-only plan, entry point without Python. They run in temporary folders whose names contain spaces and brackets and never touch the live project.

**Real installation rehearsal (manual, not a test; results below).** A copy of this branch in a scratch folder was set up for real, with downloads from PyPI, GitHub and Hugging Face, using a temporary HOME. It is **not a clean-Mac rehearsal**: this Mac already has Homebrew, Python 3.14/3.12, ffmpeg and Claude Code installed, and its pip/Hugging Face caches were not used or cleared.

Recorded 2026-11-18 on the development Mac (Apple silicon, macOS 27.2.1), in a scratch copy of the branch with a temporary HOME and the documentation address `192.0.2.10`:
* `setup-mac.sh apply` exited 0. It created the three environments from the lock files over the real network (36 + 29 + 3 packages, all installed with `--require-hashes`, including the phonikud archive from GitHub) and every smoke import loaded. It downloaded the voice files (3 files, 371 MB) and the Whisper model (weights 1.6 GB + config) from Hugging Face; **every file matched the size and SHA-256 pinned in `requirements/models.json`**, which is the first real confirmation of those pins. Disk used: 3.4 GB.
* `tools/preflight.py --hashes` on the result: nothing required missing; the pinned Whisper copy and the model checksums verified. (Warnings: the documentation address is not this Mac's, and ports 8000/8001 were in use by the owner's own running assistant, which the rehearsal never touched or started.)
* A second `apply` verified everything from the real state and downloaded nothing (4.5 seconds).
* Functional check with the downloaded files only (no server, no Claude): the Hebrew voice synthesised a sentence, and the pinned local Whisper copy transcribed it back to the same sentence.
* `remove` showed its plan, then `remove --apply` deleted the environments and the downloaded model files (3.4 GB down to 5.5 MB); decoy `data/`, `secrets/`, `backups/`, the configuration file and a user's own note in the model folder were untouched, and the tool said that folder was kept because it was not empty.
* **Not exercised:** a Mac without Homebrew/Python/ffmpeg/Claude (the guidance paths are covered only by the simulated cases), a cold network cache, starting the server or the Stick, Claude login, the firewall prompt, and any Mac other than this one.

## Open gaps (recorded after the milestone; nothing below is solved yet)
The kit's overall limitations are listed in docs/getting-started.md; the gaps below are the host-setup details.

**Clean-Mac gaps**
1. No rehearsal on a Mac (or macOS user account) that has no Homebrew, Python, ffmpeg or Claude Code yet. The "missing prerequisite" guidance is exercised only by simulated cases.
2. No rehearsal with a cold network cache or behind a proxy/VPN; pip honours the user's own pip configuration, which was not varied.
3. The interactive address chooser needs a terminal and has no automated test.
4. macOS prompts (firewall "accept incoming connections", Gatekeeper/quarantine for a downloaded copy, Terminal permissions) have not been observed on a fresh Mac.
5. Only macOS 27.2.1 on one Apple-silicon model was used; other macOS versions and Macs are untested (Claude Code itself documents macOS 13+).
6. The setup was never followed by starting the server from the new installation, Claude login, or a Stick on it.

**Reproducibility gaps**
1. Packages built from source (`docopt`, `phonikud`) fetch their build tools through pip's isolated build, which is not hash-checked.
2. The Python interpreters and the `pip` bundled in each new environment come from the user's Python, not from a pinned artifact.
3. Homebrew packages (Python, ffmpeg) and Claude Code are whatever their installers deliver.
4. The lock files accept any published file of a pinned version (not one specific wheel).
5. The phonikud archive hash depends on GitHub serving byte-identical archives (a change fails closed).
6. The model hashes were confirmed by one real download; licences of models and packages remain under review.
7. Lock files target Python 3.14 / 3.12 on macOS and must be regenerated by the maintainers when pins change.

## Known limits
No installer package or double-clickable app yet; no Stick preparation (firmware, backup, staged activation); no Claude/account handling; no sandbox for the setup process itself; `remove` does not undo Homebrew/Python/Claude installs you made.
