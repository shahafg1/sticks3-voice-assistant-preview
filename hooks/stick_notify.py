#!/usr/bin/env python3
"""Claude Code hook: tell the Stick a session finished a turn or needs attention.

settings.json runs:  stick_notify.py finished     (Stop hook)
                     stick_notify.py attention    (Notification hook)

Only {kind, project label, session id, prompt id} is sent, to the voice
assistant server on this Mac; the transcript and message text never leave the
hook. It reads the hook JSON from stdin, uses short timeouts, prints nothing
and always exits 0 (never 2, which would block Claude Code).

Set STICK_NOTIFY_SKIP=1 in a process's environment to silence its sessions;
the voice server does this for its own `claude -p` calls.
The endpoint comes from STICK_NOTIFY_URL if set, otherwise from the host configuration
(stick_config.py, config/host.conf): the loopback-only panel listener, http://127.0.0.1:<panel_port>/hook/notify
(the LAN device port no longer accepts notifications; docs/security.md). With neither, the hook does nothing: it never guesses an
address. Set STICK_NOTIFY_DEBUG to a file path to log what the hook did.
"""
import json
import os
import signal
import sys
import time
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUEST_TIMEOUT_S = 0.8
HARD_LIMIT_S = 2
ATTENTION_TYPES = {"permission_prompt", "elicitation_dialog", "agent_needs_input"}


def debug(message):
    path = os.environ.get("STICK_NOTIFY_DEBUG")
    if not path:
        return
    try:
        with open(path, "a") as log:
            log.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {message}\n")
    except OSError:
        pass


def notify_url():
    """The endpoint to post to, or None when nothing is configured (then the hook stays silent and harmless)."""
    explicit = os.environ.get("STICK_NOTIFY_URL")
    if explicit:
        return explicit
    try:
        sys.path.insert(0, PROJECT_ROOT)            # the hook runs under `python -I -S`: add the project folder by hand
        import stick_config
        return stick_config.load(env=os.environ).notify_url
    except Exception as error:                      # noqa: BLE001 - a hook must never fail
        debug(f"no endpoint: {error.__class__.__name__}: {str(error).splitlines()[0] if str(error) else ''}")
        return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if os.environ.get("STICK_NOTIFY_SKIP"):
        debug(f"{mode}: skipped (STICK_NOTIFY_SKIP)")
        return
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    if mode == "finished":
        kind = "turn_finished"
    elif mode == "attention":
        if payload.get("notification_type") not in ATTENTION_TYPES:
            debug(f"attention: ignored type {payload.get('notification_type')!r}")
            return
        kind = "attention"
    else:
        debug(f"unknown mode {mode!r}")
        return

    url = notify_url()
    if not url:
        debug(f"{mode}: not sent (no STICK_NOTIFY_URL and no usable host configuration)")
        return
    label = os.path.basename(str(payload.get("cwd") or "").rstrip("/")) or "Claude"
    body = json.dumps({
        "kind": kind,
        "label": label,
        "session": str(payload.get("session_id") or ""),
        "prompt": str(payload.get("prompt_id") or ""),
    }).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Stick-Hook": "1"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            debug(f"{kind} {label!r}: sent, server said {response.read(200)!r}")
    except Exception as error:
        debug(f"{kind} {label!r}: not delivered ({error.__class__.__name__}: {error})")


if __name__ == "__main__":
    # Backstop: whatever goes wrong, never hold Claude Code up for more than a moment.
    signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
    signal.alarm(HARD_LIMIT_S)
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
