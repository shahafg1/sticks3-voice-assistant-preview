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
The guide, in Hebrew: **[docs/getting-started.md](docs/getting-started.md)**. It covers the setup order, the known limitations and how to go back. When something fails: **[docs/troubleshooting.md](docs/troubleshooting.md)**.

## Install with an AI assistant
You can ask an AI coding assistant (Claude Code, Codex or similar) to do the installation with you.
* **What it follows:** [AGENTS.md](AGENTS.md) (Claude Code reads it through [CLAUDE.md](CLAUDE.md)). It inspects first, shows one plan, runs the same setup tools with their safety checks, and asks you for:
  * your logins;
  * anything physical on the Stick;
  * choosing the device;
  * the final listening check.
* **What it never does:** flash firmware, replace the device key, or see your passwords.
* **What it doesn't change:** the voice assistant's own Claude call still runs isolated, and doesn't load these files.

Copy one prompt into the assistant:

**English**
```text
Download the newest pre-release of https://github.com/shahafg1/sticks3-voice-assistant-preview, verify it against its SHA256SUMS and extract it into a new folder. Before doing anything else, read AGENTS.md and the guides it lists. Then help me install it on my Mac and my StickS3: inspect the current state first, show me one plan with everything you need from me, and involve me whenever a step needs my login, my hands on the device or my confirmation. Talk to me in English.
```

**עברית**
```text
הורד את גרסת התצוגה המקדימה החדשה ביותר מ-https://github.com/shahafg1/sticks3-voice-assistant-preview, בדוק אותה מול קובץ SHA256SUMS וחלץ אותה לתיקייה חדשה. לפני כל פעולה אחרת, קרא את AGENTS.md ואת המדריכים שהוא מפנה אליהם. אחר כך עזור לי להתקין אותה על ה-Mac ועל ה-StickS3: קודם בדוק את המצב הקיים, הצג לי תוכנית אחת עם כל מה שאתה צריך ממני, ושתף אותי בכל שלב שדורש התחברות שלי, פעולה שלי על המכשיר או אישור שלי. דבר איתי בעברית.
```
Already downloaded? Open the project folder in the assistant and start the prompt from "Before doing anything else".

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
| When something fails | [troubleshooting](docs/troubleshooting.md) |

## Tests
`python3 tests/run_all.py --sandbox` runs the package's test suites (after the Mac setup, `./setup-mac.sh apply --advertise-host <address>`; prerequisites in [tests/README.md](tests/README.md)).

## Licence
MIT for this project's original code, documentation and sound effects (`LICENSE`). Third-party software, models and services keep their own licences (`THIRD_PARTY_NOTICES.md`).
