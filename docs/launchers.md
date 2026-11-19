# The two desktop launchers: what is verified, and what is not

`Start Voice Assistant.command` starts the server in a Terminal window; `Voice Assistant Panel.command` opens the control panel in the browser, starting the server first
(through the regular launcher) if it is not running. Both take every location, address and port from the host configuration (`docs/configuration.md`).

## Scripted end-to-end validation (`tests/test_launchers_e2e.py`)
**This is not a Finder double-click.** The suite runs the launcher scripts with `zsh`, as identical copies under neutral names (the isolation guard refuses to start anything named like the
real launchers), against the **real `audio_server.py`** in a temporary copy of the project:

* the project folder is named `Voice Assistant (copy) [e2e] v1` (spaces, parentheses, brackets), in a temporary directory; HOME, data, secrets and recordings are temporary;
* ports are ephemeral and on 127.0.0.1; the Stick, the real Claude, real transcription and real speech synthesis are replaced by stand-ins (a script, a stub module, a tiny WAV writer);
* `open` is a recording stand-in: a URL is only written to a log (no browser opens); `open -a Terminal <launcher>` runs the neutral copy of that launcher in the background, as Terminal would.

| Case | Verified |
|---|---|
| Nothing running | the launcher starts exactly one server; the stand-in pipeline answers an upload; the panel answers with its status JSON |
| Correct server already running | the second run of the same launcher says so, exits 0 and starts nothing (same PID set); the panel launcher opens the configured loopback URL and starts nothing |
| Panel launcher, server not running | asks Terminal to run the regular launcher once, waits for the panel, opens `http://127.0.0.1:<panel_port>/`; pressing it again creates no second server and no second Terminal request |
| Device port held by an unrelated process | both launchers report the port and the process, exit 1, start nothing, and leave that process alone; the panel launcher does **not** call it "the server" |
| Panel port held by an unrelated web server | it is not mistaken for the panel; the browser is not opened to it |
| Configuration missing / invalid (bad address, port out of range, two ports equal, broken file) | actionable message on stderr (no Python traceback, no author address), non-zero exit, nothing started, nothing opened |
| Server environment missing | named in the message; nothing started |
| Binding | the server's listeners are on the configured loopback addresses only, never on `*` |

### Defects this validation found (fixed in the launchers)
1. The panel launcher treated **any** HTTP answer on the panel port as "the panel is up" and opened the browser to it. It now requires this panel's status JSON.
2. It treated **any** listener on the device port as "the server is running" and then blamed the panel. A running server is now recognised only by its own process; any other listener is reported as a busy port.
3. With an incomplete configuration it asked Terminal to start a server that could not start, then waited 90 seconds. It now validates the complete configuration first and says what is wrong at once.

## What this does **not** prove
* Finder double-click behaviour, Terminal.app opening a `.command` file, Gatekeeper/quarantine prompts for a downloaded copy, and macOS privacy prompts.
* A real browser opening the URL (the stand-in only records it).
* The real Claude, transcription and speech models, and the Stick.

## Physical check for the owner (not done)
On a **copy** of the project with non-production ports (so the live server is not disturbed): double-click `Voice Assistant Panel.command` in Finder with no server running → a Terminal window appears,
the server starts, and the browser opens the panel; double-click it again → the browser opens the panel and no second server window appears; double-click `Start Voice Assistant.command` while the server runs →
it says the assistant is already running and starts nothing.
