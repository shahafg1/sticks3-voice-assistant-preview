#!/bin/zsh
# Uploads the current Stick sources and the device configuration to the Stick, verifies each hash on the device, and resets it.
#   tools/deploy_stick.sh   uploads build/device_config.json and every app module the Stick runs (FILES below, in that order), then resets
# Locations (mpremote, USB port) and the server address come from the host configuration (docs/configuration.md). The Stick's own
# configuration is generated from it (server address and port only) and is uploaded with the code; without a valid host configuration this
# script stops BEFORE touching the Stick. mpremote interrupts the running app; the Stick is reset at the end.
# This is a development tool. A first installation of the complete file set (with sounds, staging, a backup check and a record)
# is tools/stick_install.py (docs/stick-install.md); keep FILES below and stick/install_manifest.json in step (tests check it).
setopt NO_UNSET PIPE_FAIL
ROOT="${0:A:h:h}"
cd "$ROOT" || exit 1
CONFIG_OUTPUT="$(python3 "$ROOT/stick_config.py" shell)" || exit 2      # (eval "$(...)" would hide a failure of the command inside)
eval "$CONFIG_OUTPUT"
MPREMOTE="$CFG_MPREMOTE"
PORT="$CFG_USB_PORT"
[[ -n "$PORT" ]] || { echo "No USB port to use: $CFG_USB_NOTE"; exit 2; }
[[ -x "$MPREMOTE" ]] || { echo "mpremote not found: $MPREMOTE (set tools_env or mpremote in the configuration)"; exit 2; }
DEVICE_CONFIG="$ROOT/build/device_config.json"
python3 "$ROOT/stick_config.py" device-config --out "$DEVICE_CONFIG" || exit 2
# ORDER: the upload is NOT atomic (each file is replaced on its own; a failure stops before the reset, and the Stick keeps running whatever
# mix it then holds until it restarts). The order is RISK REDUCTION, not a recovery guarantee: it follows these rules, which are checked
# against the source (tests/test_config.py), but no intermediate mix has ever been started to prove it works. Recovery after a failed
# upload is running this tool again, or restoring from a backup taken first (tools/stick_backup.py backup):
#  1. modules that older Sticks do not have, before anything imports them: server_config.py (new voice_send.py), mic.py (new voice_send.py),
#     statick_shapes.py (new robot.py); shake.py is unchanged and listed so a Stick missing it gets it;
#  2. voice_send.py works with the old or new device.py (it passes the server address explicitly); device.py, sleep.py, reminder.py and
#     robot.py keep their interfaces;
#  3. notify.py after device.py, because the OLD device.py still imports HOST and PORT from it;
#  4. main.py last: it only adds traceback logging around the app.
# A first installation is tools/stick_install.py; the boot option, Wi-Fi and the device key are not part of either tool.
FILES=(server_config.py mic.py statick_shapes.py shake.py voice_send.py device.py sleep.py reminder.py robot.py notify.py main.py)
device() { "$MPREMOTE" connect "$PORT" resume "$@" 2>&1 | tr -d '\r'; }
upload() {   # upload FILE_ON_MAC NAME_ON_DEVICE
    device fs --no-verbose cp "$1" ":/flash/$2" >/dev/null
    want=$(shasum -a 256 "$1" | awk '{print $1}'); have=$(device fs sha256sum ":/flash/$2" | tail -1 | awk '{print $1}')
    [[ "$want" == "$have" ]] || { echo "Verification failed for $2. Stopping (the Stick was not reset)."; exit 1; }
    echo "  deployed $2"
}
upload "$DEVICE_CONFIG" device_config.json
for f in $FILES; do upload "stick/$f" "$f"; done
device exec --no-follow "import machine; machine.reset()" >/dev/null
echo "Done: hashes verified, Stick reset."
