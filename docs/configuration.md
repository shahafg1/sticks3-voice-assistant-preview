# Host configuration

Everything that is specific to one Mac (the address the Stick uses, ports, locations of tools) lives in **one small file**, `config/host.conf`, plus optional
environment variables. The project folder itself can be anywhere, including a path with spaces: every location is derived from where the files are installed.
Hebrew and the `Asia/Jerusalem` time zone are the explicit product scope for now and are **not** configurable.

## Setting up (new installation)
1. Copy `config/host.example.conf` to `config/host.conf` (it is git-ignored and never committed).
2. Set `advertise_host` to this Mac's address on the Wi-Fi network the Stick joins. List the Mac's addresses (read-only): `python3 stick_config.py suggest-address`.
   Give the Mac a fixed address (a reservation in the router); if it changes, the Stick cannot find the server.
3. Check it: `python3 tools/preflight.py` (read-only; see `docs/preflight.md`).
4. Generate the Stick's own file when you deploy: `tools/deploy_stick.sh` does it (or `python3 stick_config.py device-config --out build/device_config.json`).

There is **no default address**. A server, launcher, hook or tool started without a usable configuration says exactly what to do and stops; it never falls back to a
built-in address.

## The file (`config/host.conf`, INI format, `#` comments)
| Section / key | Meaning | Default | Environment variable |
|---|---|---|---|
| `[server]` `advertise_host` | The address the **Stick** uses to reach this Mac (IPv4, or a host name). **Required.** Never `127.0.0.1` / `0.0.0.0`. | none | `STICK_ADVERTISE_HOST` |
| `[server]` `bind_host` | The address the **server listens on** (IPv4, or `0.0.0.0` for every interface). Needed if `advertise_host` is a host name. | `advertise_host` when that is an IPv4 address (listen on that one address only, as before) | `STICK_BIND_HOST` |
| `[server]` `device_port` | TCP port of the Stick's API; the Stick's configuration carries the same number | 8000 | `STICK_DEVICE_PORT` |
| `[server]` `panel_port` | TCP port of the browser panel | 8001 | `STICK_PANEL_PORT` |
| `[device]` `usb_port` | USB serial device used by the Stick tools. Empty: auto-select when exactly one `/dev/cu.usbmodem*` device is connected, otherwise the tool stops and asks you to set it | empty | `STICK_PORT` |
| `[paths]` `voice_env`, `tts_env`, `tools_env` | Python environments (server; Hebrew TTS; the folder whose `bin/` holds the Stick tool) | `voice-env`, `tts-env312`, `tools-env` | `STICK_VOICE_ENV`, `STICK_TTS_ENV`, `STICK_TOOLS_ENV` |
| `[paths]` `mpremote` | Explicit Stick tool program | `<tools_env>/bin/mpremote` | `MPREMOTE` |
| `[paths]` `claude`, `ffmpeg` | Claude Code CLI / ffmpeg. A bare name is looked up on `PATH` | looked up on `PATH` | `STICK_CLAUDE`, `STICK_FFMPEG` |
| `[paths]` `models_dir`, `whisper_dir`, `data_dir`, `secrets_dir`, `recordings_dir` | Model files; settings database and reminder audio; the device key; the latest recording/transcript/reply | `tts-models`, `whisper-model`, `data`, `secrets`, `recordings` | `STICK_MODELS_DIR`, `STICK_WHISPER_DIR`, `STICK_DATA_DIR`, `STICK_SECRETS_DIR`, `STICK_RECORDINGS_DIR` |
`STICK_CONFIG` points to a different configuration file (it must exist). Relative paths are relative to the project folder; `~` is expanded.

**Precedence for every value: environment variable > config file > built-in default.** An empty variable counts as "not set".

**Validation** (all problems are reported together, with the file name): `advertise_host` required, not loopback/wildcard, IPv4 or a valid host name; `bind_host` an IPv4 address;
ports whole numbers 1024-65535 and different from each other; `usb_port` must look like `/dev/cu.…`; unknown sections or settings are errors (a typo is never ignored silently).

**Bind address versus advertised address.** The server listens on `bind_host`; the Stick connects to `advertise_host`. By default they are the same, so nothing is exposed beyond
the one address used today. **The panel is always loopback-only (`127.0.0.1`)**: it has no address setting, and its Host/Origin/CSRF checks and authentication are unchanged.
A program on this Mac (the notification hook) reaches the server on `bind_host`, or on `127.0.0.1` when the server listens on `0.0.0.0`.

## The Stick's configuration (separate file)
`python3 stick_config.py device-config [--out FILE]` writes `{"v": 1, "server_host": "...", "server_port": 8000}` — **only** the address and port the Stick needs. It never contains host paths,
Wi-Fi credentials or the device key (that has its own file, `device_key.txt`, provisioned by `tools/stick_setup.py key`). It is uploaded as `/flash/device_config.json` (by `tools/deploy_stick.sh`).
The Stick code (`stick/server_config.py`) validates it; with the file missing or invalid the Stick shows **"No server / Setup needed"** and sends nothing anywhere.

## Upgrading an existing installation (do this before restarting the server or updating the Stick)
Upgrading an older installation step by step is outside this preview's documentation.
An installation made before this change used a built-in address and has no `config/host.conf`, so after updating the code the server, launchers and tools stop with the message above until you create it.
1. Decide the address your Stick already uses (the Mac's LAN address). `python3 stick_config.py suggest-address` lists candidates; nothing is detected silently.
2. `python3 stick_config.py init --advertise-host <that address> [--keep-existing-locations]` writes `config/host.conf` (it refuses to overwrite an existing file and validates before writing).
   `--keep-existing-locations` also records an existing Stick-tools folder at `~/sticks3-env`, the previous default location.
   `--project <folder>` writes the file into another project folder (so an installation can be prepared **before** its code is switched; the old code ignores the file, and the hook never sees a gap).
3. `python3 tools/preflight.py` to check. Restart the server with the launcher when you are ready.
4. The Stick keeps working with the code already on it. **New Stick code needs the device configuration**: deploy with `tools/deploy_stick.sh` (it uploads `device_config.json` first and then the code, in an order meant to reduce the risk of an interrupted upload, which is not a guarantee and was never tested with a partial upload, and refuses to start without a valid configuration).
   Until then an old Stick and a new server work together as before (the protocol did not change).

## Where it is used
`audio_server.py` (bind address, ports, folders, tool locations), `tts.py`, `Start Voice Assistant.command` and `Voice Assistant Panel.command` (project folder, ports, locations; checked before anything starts),
`hooks/stick_notify.py` (endpoint; with no configuration and no `STICK_NOTIFY_URL` the hook silently does nothing), `tools/deploy_stick.sh`, `tools/stick_setup.py`, `restore_stick_startup.sh`
(tool and USB selection), `stick/voice_send.py` + `stick/server_config.py` (the Stick). Not changed: the per-version rollback scripts `tools/rollback_to_v0_*.sh` (they belong to older tags and still assume the old layout).

## Limits of this milestone
Installation, device backup and setup are handled by the tools described in docs/getting-started.md; firmware and the Claude account are not.
