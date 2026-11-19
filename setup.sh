#!/bin/bash
# The one entry point for setting up the assistant:   ./setup.sh [plan | run | status | forget]   (no argument = plan: read-only)
# It only finds a Python 3.9+ to run the wizard with, WITHOUT triggering macOS's "install the command line developer tools" pop-up
# (the /usr/bin/python3 that ships with macOS is only a stub until those tools are installed), and then hands over to tools/setup_wizard.py. It installs nothing.
HERE="$(cd "$(dirname "$0")" && pwd -P)"

usable() { [ -n "$1" ] && [ -x "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; }

candidates=()
if [ -n "${STICK_SETUP_PYTHON_CANDIDATES:-}" ]; then
    IFS=':' read -r -a candidates <<< "$STICK_SETUP_PYTHON_CANDIDATES"
else
    found="$(command -v python3 2>/dev/null)"
    [ -n "$found" ] && candidates+=("$found")
    candidates+=(/opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3)
fi

for candidate in "${candidates[@]}"; do
    if [ "$candidate" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then
        continue            # the stub: running it would open an installer window
    fi
    if usable "$candidate"; then
        exec "$candidate" "$HERE/tools/setup_wizard.py" "${@:-plan}"
    fi
done

cat >&2 <<'MSG'
The guided setup needs Python 3.9 or newer to start, and none was found (nothing was changed).
Pick ONE way to get it, then run this again:
  - Homebrew:   brew install python@3.14        (https://formulae.brew.sh/formula/python@3.14; Homebrew itself: https://brew.sh)
  - Installer:  https://www.python.org/downloads/macos/
  - Apple's command line tools:   xcode-select --install     (this opens a window and takes a few minutes)
MSG
exit 2
