#!/bin/zsh
# Opens the voice assistant's control panel in the browser (Hebrew, local to this Mac).
# If the server is not running it is started with the regular launcher, which refuses
# to start a second copy.

# Everything machine-specific comes from config/host.conf (docs/configuration.md); the project folder is wherever this file lives (spaces are fine).
PROJECT="${0:A:h}"
SERVER_LAUNCHER="$PROJECT/Start Voice Assistant.command"

pause_and_exit() {
    echo
    read -r "?לחץ Enter כדי לסגור את החלון..."
    exit "$1"
}

CFGPY="$(command -v python3)" || { echo "python3 not found: install Python 3 (see docs/configuration.md)."; pause_and_exit 1; }
CONFIG_OUTPUT="$("$CFGPY" "$PROJECT/stick_config.py" shell --no-server)" || pause_and_exit 1
eval "$CONFIG_OUTPUT"
PANEL_PORT="$CFG_PANEL_PORT"
PANEL_URL="http://127.0.0.1:$PANEL_PORT/"
SERVER_PORT="$CFG_DEVICE_PORT"

# The panel is up only if the answer is this panel's status JSON: any other web server on that port must not be mistaken for it.
panel_up() { curl -fsS -m 2 "http://127.0.0.1:$PANEL_PORT/api/status" 2>/dev/null | grep -q '"key_provisioned"'; }
# A running server is a Python process whose command line ENDS with this project's script path. (A plain substring match would also
# hit an editor or a shell that merely mentions the file.) Something else listening on the device port is NOT this server.
server_running() {
    local pattern
    pattern="${${(b)PROJECT}//./\\.}/audio_server\\.py"
    pgrep -f "/Python +(-[A-Za-z]+ +)*${pattern}\$" >/dev/null
}

if panel_up; then
    open "$PANEL_URL"
    exit 0
fi

if server_running; then
    echo "השרת פועל, אבל הפאנל לא עונה על $PANEL_URL"
    echo "ייתכן שהפאנל כבוי (STICK_PANEL=0), שהפורט $PANEL_PORT תפוס, או שמסד הנתונים לא נפתח."
    echo "ההודעה המפורטת מופיעה בחלון השרת (שורה שמתחילה ב-[panel])."
    pause_and_exit 1
fi

listener=$(lsof -nP -iTCP:$SERVER_PORT -sTCP:LISTEN 2>/dev/null)
if [[ -n "$listener" ]]; then
    echo "שגיאה: פורט $SERVER_PORT תפוס על ידי תהליך אחר (לא העוזר הקולי). התהליך לא נסגר:"
    echo "$listener"
    pause_and_exit 1
fi

# Starting the server needs the complete configuration (advertise_host, environments): say so now instead of waiting for a window that cannot come up.
"$CFGPY" "$PROJECT/stick_config.py" shell >/dev/null || pause_and_exit 1

echo "השרת לא פועל. מפעיל אותו עם המפעיל הרגיל (הוא לא יפעיל עותק שני)..."
if [[ ! -e "$SERVER_LAUNCHER" ]]; then
    echo "לא נמצא המפעיל: $SERVER_LAUNCHER"
    pause_and_exit 1
fi
open -a Terminal "$SERVER_LAUNCHER"
for i in {1..90}; do
    panel_up && { open "$PANEL_URL"; exit 0; }
    sleep 1
done
echo "הפאנל לא עלה תוך 90 שניות. בדקו את חלון השרת."
pause_and_exit 1
