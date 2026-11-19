#!/usr/bin/env python3
"""Add or remove the Stick notification hooks in a Claude Code settings file.

  manage_claude_hooks.py status
  manage_claude_hooks.py install     back up settings.json, then add the two hooks
  manage_claude_hooks.py uninstall   back up settings.json, then remove only those hooks

Only entries whose command mentions hooks/stick_notify.py are touched; all
other hooks and settings are left exactly as they are. Installing twice does
nothing the second time. Options: --settings PATH (default ~/.claude/settings.json).
"""
import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = "hooks/stick_notify.py"
ATTENTION_MATCHER = "permission_prompt|elicitation_dialog|agent_needs_input"
DEFAULT_PYTHON = HERE.parent / "voice-env" / "bin" / "python"


def command_for(mode, python):
    # "|| true": even a missing interpreter must not show a hook error in Claude Code.
    return f"{python} -I -S {HERE / 'stick_notify.py'} {mode} || true"


def is_ours(hook):
    return MARKER in str(hook.get("command", ""))


def add_hooks(settings, python):
    """Append our two hook groups (if absent). Returns the number of groups added."""
    hooks = settings.setdefault("hooks", {})
    added = 0
    wanted = {
        "Stop": {"hooks": [{"type": "command", "command": command_for("finished", python), "timeout": 3}]},
        "Notification": {"matcher": ATTENTION_MATCHER,
                         "hooks": [{"type": "command", "command": command_for("attention", python), "timeout": 3}]},
    }
    for event, group in wanted.items():
        groups = hooks.setdefault(event, [])
        if not any(is_ours(h) for g in groups for h in g.get("hooks", [])):
            groups.append(group)
            added += 1
    return added


def remove_hooks(settings):
    """Remove only our hooks, dropping groups/events/sections left empty. Returns number removed."""
    removed = 0
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept_groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if not is_ours(h)]
            removed += len(group.get("hooks", [])) - len(kept)
            if kept:
                kept_groups.append({**group, "hooks": kept})
            elif not group.get("hooks"):
                kept_groups.append(group)      # not ours and already empty: leave as found
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]
    return removed


def load(path):
    raw = path.read_text()
    return json.loads(raw), raw


def dump(settings):
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def backup(path):
    target = path.with_name(f"{path.name}.bak-stick-notify-{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, target)
    return target


def write_atomically(path, text):
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("status", "install", "uninstall"))
    parser.add_argument("--settings", default="~/.claude/settings.json")
    parser.add_argument("--python", default=str(DEFAULT_PYTHON), help="interpreter used by the hooks")
    args = parser.parse_args(argv)
    path = Path(args.settings).expanduser()

    try:
        settings, raw = load(path)
    except (OSError, ValueError) as error:
        print(f"Cannot read {path}: {error}. Nothing changed.")
        return 1

    installed = sum(is_ours(h) for groups in settings.get("hooks", {}).values()
                    for g in groups for h in g.get("hooks", []))
    if args.action == "status":
        print(f"{path}: Stick notification hooks {'installed (%d)' % installed if installed else 'not installed'}")
        for b in sorted(path.parent.glob(path.name + ".bak-stick-notify-*")):
            print("  backup:", b)
        return 0

    updated = copy.deepcopy(settings)
    if args.action == "install":
        if not Path(args.python).exists():
            print(f"Interpreter not found: {args.python}. Nothing changed.")
            return 1
        changed = add_hooks(updated, args.python)
        message = f"added {changed} hook group(s)"
        # Safety net: undoing the change must give back exactly what we started with.
        check = copy.deepcopy(updated)
        remove_hooks(check)
        baseline = copy.deepcopy(settings)
        remove_hooks(baseline)
        if check != baseline:
            print("Internal check failed (uninstall would not restore the original). Nothing changed.")
            return 1
    else:
        changed = remove_hooks(updated)
        message = f"removed {changed} hook(s)"

    if not changed:
        print(f"Nothing to do: {message}.")
        return 0
    saved = backup(path)
    write_atomically(path, dump(updated))
    json.loads(path.read_text())       # must still parse
    print(f"{args.action}: {message}. Backup: {saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
