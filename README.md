# StickS3 Hebrew voice assistant (preview)

A Hebrew voice assistant built from an M5Stack **StickS3** and a Mac.
1. You press the button and ask.
2. The Mac transcribes the question locally (Whisper) and asks Claude through Claude Code (web search only).
3. It speaks the answer back in Hebrew through a local text-to-speech voice.

There's also a small control panel on the Mac (open it with `Voice Assistant Panel.command`; it is reachable from this Mac only), one-time reminders, and Claude Code "your session finished" notifications on the Stick.

> **Preview, non-commercial use only.**
> * The project's own code is MIT (`LICENSE`). The Hebrew voice that setup downloads is CC BY-NC 4.0, so the complete assistant is **not cleared for commercial use** (`THIRD_PARTY_NOTICES.md`).
> * Tested on one Mac and one Stick, and in simulation. This preview's Hebrew TTS adapter, sound effects and isolated Claude call were checked on the Mac (with the real voice models and Claude Code), but haven't yet run on a Stick. See the known limitations in `docs/getting-started.md`.

## Requirements
* An Apple silicon Mac (tested on macOS 27.2) with about 4 GB free.
* Python 3.14 and 3.12, ffmpeg, and Claude Code with a Claude account that can use it.
* An M5Stack StickS3 on **UIFlow2 V2.4.8** (MicroPython 1.27.0), and a USB-C cable.
* The Mac and the Stick on the same home Wi-Fi.

## Getting started
```sh
./setup.sh          # explains the requirements and the plan; changes nothing
./setup.sh run      # the guided setup (Hebrew); asks before each step that changes something
```
The guide, in Hebrew: **[docs/getting-started.md](docs/getting-started.md)**. It covers the setup order, the known limitations and how to go back.

## Privacy and security
* **[docs/privacy-he.md](docs/privacy-he.md)** (Hebrew) and **[docs/privacy-review.md](docs/privacy-review.md)** describe what is sent to Anthropic, what is stored locally and how to delete it.
* The Claude call runs isolated from your own Claude Code settings, memory and connectors, and Claude Code keeps no local copy of it.
* **[docs/security.md](docs/security.md)** covers the network: every request from the Stick needs a device key, but home-network traffic is **not encrypted**. Never expose the server to the internet.

## Documentation
| Topic | Document |
|---|---|
| Mac setup, configuration, preflight, launchers | [mac-setup](docs/mac-setup.md), [configuration](docs/configuration.md), [preflight](docs/preflight.md), [launchers](docs/launchers.md) |
| Stick setup: Wi-Fi, key, auto-start, pairing | [stick-setup](docs/stick-setup.md) |
| Installing, backing up and restoring the Stick's files | [stick-install](docs/stick-install.md), [stick-backup](docs/stick-backup.md), [stick-restore](docs/stick-restore.md) |
| Control panel and reminders | [desktop-panel](docs/desktop-panel.md), [reminders](docs/reminders.md) |

## Tests
`python3 tests/run_all.py --sandbox` runs the package's test suites (after `./setup-mac.sh apply`; prerequisites in [tests/README.md](tests/README.md)).

## Licence
MIT for this project's original code, documentation and sound effects (`LICENSE`). Third-party software, models and services keep their own licences (`THIRD_PARTY_NOTICES.md`).
