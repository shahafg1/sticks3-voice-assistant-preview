# Setup preflight (read-only)

```
python3 tools/preflight.py                 short report for people
python3 tools/preflight.py --json          the same as JSON (schema 1) for tools
python3 tools/preflight.py --hashes        also verify model checksums (reads about 2 GB; a few seconds)
python3 tools/preflight.py --no-usb        skip listing USB devices
python3 tools/preflight.py --network       opt in: open a TCP connection to PyPI, GitHub and Hugging Face (3 s limit each, nothing sent)
python3 tools/preflight.py --probe-server  opt in: open a TCP connection to the configured ports (nothing sent, no job created)
```
Exit status: 0 = nothing required is missing, 1 = something required is missing or invalid, 2 = bad command line. It uses only the Python standard library, so it runs before any environment exists.

## Checking an installation whose environments are not under the project folder
Defaults are relative to the folder the code is in, so code in a separate checkout or worktree looks for its own `voice-env/`, `tts-env312/`… and reports them missing. Point it at the installed ones
with environment variables **attached to the Python command with `env`**, all on **one line**:

```
env STICK_ADVERTISE_HOST=<your Mac's address> STICK_VOICE_ENV="$HOME/<project>/voice-env" STICK_TTS_ENV="$HOME/<project>/tts-env312" STICK_TOOLS_ENV="$HOME/<tools-env>" STICK_MODELS_DIR="$HOME/<project>/tts-models" python3 tools/preflight.py
```
Why `env`: a line that only assigns `NAME=value` sets a *shell* variable, which a program started on the next line does **not** receive. `env NAME=value … python3 …` hands the values to that one process
and changes nothing else. The report line **"Locations in effect"** lists each location with its source: `[env]` (the variable reached the process), `[file]` (`config/host.conf`) or `[default]` (derived from the
code's folder). If a location you set shows `[default]`, the variable did not arrive. Nothing is written or exported anywhere.

## What it does and does not do
It **never** installs or downloads anything, creates or changes application state (no database, no settings, no config file, no Python bytecode), migrates a database, starts the server, runs Claude,
reads account data or private recordings, or opens a serial connection. USB devices are only listed (device names in `/dev` plus vendor/product metadata from the system, never serial numbers); a running Stick is not interrupted.
Network probes are opt-in and bounded. The only commands it runs are read-only system queries (`sysctl`, `lsof`, `ifconfig`, `system_profiler`) and the **environments' own Python** to list installed package names and versions.

## What it checks
| Group | Items |
|---|---|
| **Required** to run | Apple silicon and native arm64 Python; host configuration valid (and what addresses/ports it implies; whether the addresses belong to this Mac; whether the ports are already in use); Claude Code CLI and ffmpeg found (not run); the three Python environments and their pinned packages (`requirements/*.txt`); Hebrew TTS model files (size; checksum with `--hashes`, against `requirements/models.json`); the Whisper model in the Hugging Face cache (a missing one is a warning: it is downloaded on first use) |
| **Setup only** | `git`, `python3.14`, `python3.12` (needed to create environments / install the phonemizer, not to run) |
| **Optional** | launcher scripts present and executable, Homebrew, USB devices (listed), configured `usb_port` present, network probes |
| **Development** | Node.js |
| **Notes** | tested-platform label, ports in use, free disk and RAM |

**Untested platforms.** The project has only ever run on one combination (recorded in `tools/preflight.py`). Anything else is labelled `UNTESTED`; that is a record of what was tested, **not a minimum requirement**.
RAM and free disk are reported but never turned into a pass/fail minimum; the only disk warning compares free space with the size of the components that are actually missing.

## What it does NOT check (also printed in every report)
Stick firmware version and identity (the Stick is never contacted); whether the Stick has Wi-Fi saved or runs this software; Claude Code login, plan, limits and connectors (the CLI is not run); Claude Code hooks in
your global settings and desktop shortcuts; end-to-end connectivity (unless `--probe-server`, which only opens a connection); macOS firewall prompts, the Mac's sleep settings, the router's address assignment; licence terms (pending review).

## Tests
`tests/test_preflight.py` runs it as a real process inside a project copy with spaces in its path, with stand-in environments and tiny stand-in model files, under the isolation guard: ready/missing/invalid cases, alternate ports,
busy ports, untested platforms, USB metadata, opt-in probes, and a before/after snapshot proving it changed nothing.
