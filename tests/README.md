# Tests

Everything in `tests/test_*.py` is **automated and isolated**: it uses temporary directories, ephemeral ports on 127.0.0.1, stubs for
`claude`, Whisper and the TTS, and never needs the Stick, the live server, your database or your Claude settings.

* `isolation_guard.py` is imported first by every suite. It blocks (before the operation happens) any access to the project's
  `data/`, `secrets/`, `recordings/`, `~/sticks3-project/...`, `~/.claude`, the Stick's `/flash` and serial ports, the live service
  (this Mac's LAN address, ports 8000/8001), the real `claude` CLI, mpremote and the desktop launcher. Python children started by the tests load it
  through `guard_site/sitecustomize.py`. `test_isolation.py` proves the guard works and that defaults which point at production are trapped.
* `python tests/run_all.py` runs all suites under the guard. `python tests/run_all.py --sandbox` first copies the project to a temporary
  directory with its own HOME and TMPDIR and fails if anything creates `data/`, `secrets/`, `recordings/` or `~/.claude` there.
* The guard is a regression safeguard, not an OS sandbox, and does not claim complete isolation of every subprocess.
* Known gap: the notification hook tests run the hook with `python -I -S` (no site customization), so the guard cannot see inside it;
  they only ever give it a loopback URL (asserted in the test).

**Hardware and manual checks do not live here.** Anything that talks to a real Stick (mpremote, USB serial), the running server, the desktop
launcher or the real panel database is a manual procedure written in the milestone docs (`docs/*.md`, "physical checks"), and is
blocked by the guard if it is attempted from a test. Do not name such scripts `test_*.py`. Run suites only through `tests/` (never `pytest .` from the root).

## Running the tests from a release package
```sh
./setup-mac.sh apply                  # once: creates voice-env, the Python the server tests need (numpy, scipy)
python3 tests/run_all.py --sandbox    # every suite in the package; prints "N/N suites passed" and exits 0
```
**What it does:** `--sandbox` copies the package (without your `config/host.conf`, environments, data, secrets or recordings) into a temporary folder with its own HOME, and runs every suite there under the isolation guard. It works without git.

**Prerequisites:**
* the server environment, `voice-env`, created by `./setup-mac.sh apply`. You can instead point `STICK_TEST_PYTHON` at any Python 3.14 with numpy and scipy;
* `zsh` (the macOS default shell);
* Node.js, for the panel-script and Statick-geometry checks. Without it those two suites fail with a clear message; they are never skipped silently.

**What the package tests cover:** installation, configuration, preflight, setup, backup and restore, recovery, the network authentication and the runtime. They need nothing outside the package: no device, no live server, no private files and no git history.

**Development-history suites** (`tests/test_history_*.py`) are **not in the package**. They check upgrades and rollbacks from earlier versions (v0.7.0 to v0.11.1) against this project's own git tags, and files only the developers' repository has (retired tools, old rollback scripts, the list of the author's former built-in values). They do not apply to a fresh export, and they run in the developers' repository, where `run_all.py` includes them.

`test_hebrew_tts.py` checks the logic of `hebrew_tts.py`, the project's own TTS adapter (its input encoding, scaling and failures), without the voice models. The adapter with the real models was checked by hand on 2026-11-19 on the development Mac. Four Hebrew sentences were synthesized and transcribed back with the local Whisper model, and each came back as the same sentence. That check also established the voice's input layout: no padding between symbols. It is a manual check, because the models are not in the repository.
`test_identity.py` checks the assistant's name in the prompts the real server builds (a stand-in for `claude`: prompt construction only, never a real model answer) and `test_statick_prototype.py` the character prototype's geometry and honesty rules; neither says anything about the real Stick screen or a real conversation.
`test_mic.py` checks the microphone buffer-lifetime rules (`stick/mic.py`) against a simulated driver, plus source checks on `voice_send.py`, `main.py` and the deploy order; it says nothing about the real microphone, which was validated on the Stick separately (docs/mic-buffer-lifetime.md).

**Fixtures.** `tests/fixtures/` holds data the suites read: values that used to be built into the code (`legacy_values.txt`, so no test source file contains a live address or a home folder), a simulated Stick tool
for the deploy-script test and a sample of USB metadata. They exist so the static rules over `test_*.py` stay as strict as before; the guard still blocks the real thing at run time.
`test_stick_backup.py` covers Stick identification and the private file backup (`tools/stick_backup.py`) against a **simulated** Stick (`fixtures/fake_stick_device.py`, device nodes as files in a temporary folder via `STICK_DEV_DIR`): ports, firmware support, presence-only Wi-Fi, key exclusion, verification, disconnects, partial reads, disk space and killed runs. It runs the tool's device-side snippets in CPython, which says nothing about real MicroPython or a real Stick.

`test_stick_restore.py` covers the file restore (`tools/stick_restore.py`) with **synthetic** backups only. Each case makes a backup of the simulated Stick with `stick_backup.py`, changes the simulated Stick, then plans, compares or applies. For these cases the simulated Stick is writable, logs every write, and its rename never replaces an existing file. Faults simulate a failed or damaged copy, a disconnect, a cut-off activation, a killed run and a file damaged after activation. Nothing here writes to a real Stick or reads a real backup.

`test_stick_install.py` covers the first-install file deployment (`tools/stick_install.py`) on **synthetic** Sticks: a fresh UIFlow Stick, the same without UIFlow's `main.py`, a current installation, and an old installation built from the `v0.11.0` tag's files (`git show`, so the tags must be present, as in the sandbox's clone). It checks that the install manifest matches `tools/deploy_stick.sh`, `stick/sounds/` and the pinned hashes, so a reviewed change to a Stick file needs `python3 tools/stick_install.py pin`.

`test_stick_setup.py` covers Wi-Fi checks, device-key provisioning, auto-start and pairing (`tools/stick_setup.py`). The simulated Stick also simulates NVS writes (with failure faults), a Wi-Fi radio, a socket and a virtual clock, and flags any key or Wi-Fi value that would leave it. The "server" is the real `device_link.DeviceLink` on temporary folders, answering `/api/status` on a free local port; after each restart the tool sends, the simulated Stick reports to it only if it really could start the app (boot option 0, `main.py`, Wi-Fi saved and joinable, server reachable), authenticated with the key file on its simulated `/flash`. Keys are random per test; Wi-Fi values are sentinels.

`test_setup_wizard.py` drives the guided setup (`tools/setup_wizard.py`) like a person at the keyboard: it reads until the wizard asks (a question ends with `» `) and answers. Everything runs in a copy of the project in a temporary folder: the real `setup_mac.py` (with `test_setup_mac.py`'s local wheel and model server), the real Stick tools against the simulated Stick, and an isolated server process (`fixtures/fake_assistant_server.py`, the real `DeviceLink`). That server is started through a stand-in for macOS `open`, and its command line ends with the copy's `audio_server.py`, the way the launchers recognise this folder's server. Progress notes go to a temporary folder outside the copy.

`test_net_auth.py` checks network authentication (docs/security.md) against the real `audio_server.py`, using `test_panel_server.py`'s harness with stand-in Claude and TTS and a Whisper stub that logs its calls. "No AI work" is shown from those call logs, the recordings folder and the server's own log; every response and the log are scanned for the synthetic key. The wizard's full scenario also runs against the real secured server, with `fixtures/stick_boot_simulator.py` playing the Stick's network side after each restart.
`test_config.py` covers the host configuration, the Stick's configuration, the launchers, the hook and the deploy tool (against the simulated tool); `test_preflight.py` covers the read-only preflight.
The launchers and the deploy tool are only ever exercised as copies in temporary folders under other names (the guard refuses to start anything named like the real launchers).
`test_launchers_e2e.py` runs both desktop launchers (as neutral-name copies) against the real `audio_server.py` in a temporary project copy with spaces/parentheses/brackets in its path, on ephemeral ports, with stand-ins for Claude, transcription, TTS and `open`; it needs the server's interpreter (`STICK_TEST_PYTHON=voice-env/bin/python`) and takes a few minutes (it waits out the panel launcher's 90 s limit once). It is a scripted check, **not** a Finder double-click: see `docs/launchers.md`.
`test_setup_mac.py` covers the Mac setup tool (`tools/setup_mac.py`, `setup-mac.sh`): it **really** creates virtual environments and installs a tiny local wheel with `pip --require-hashes`, downloads from a local HTTP server (resume, truncated, corrupted answers), and uses real processes for the lock; the package index, Hugging Face and the Python versions are stand-ins. It is not a clean-Mac rehearsal; the real-network rehearsal is a manual procedure in `docs/mac-setup.md`.
