#!/bin/zsh
# Starts the M5Stack voice assistant server (audio_server.py) in this Terminal window.

# Everything machine-specific comes from config/host.conf (docs/configuration.md); the project folder is wherever this file lives (spaces are fine).
PROJECT="${0:A:h}"
SERVER="$PROJECT/audio_server.py"

# claude (npm-global) and ffmpeg (Homebrew) must be on PATH for the server's subprocesses (the usual install locations; a configured claude/ffmpeg path wins).
export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$HOME/.local/bin:$PATH"

pause_and_exit() {
    echo
    read -r "?לחץ Enter כדי לסגור את החלון..."
    exit "$1"
}

cd "$PROJECT" || { echo "שגיאה: תיקיית הפרויקט לא נמצאה: $PROJECT"; pause_and_exit 1; }

CFGPY="$(command -v python3)" || { echo "python3 not found: install Python 3 (see docs/configuration.md)."; pause_and_exit 1; }
CONFIG_OUTPUT="$("$CFGPY" "$PROJECT/stick_config.py" shell)" || pause_and_exit 1   # the message explaining what to fix was printed above
eval "$CONFIG_OUTPUT"
PYTHON="$CFG_VOICE_PYTHON"
PORT="$CFG_DEVICE_PORT"

# A server counts as running if a Python process was started with exactly this server file, or if something listens on the Stick's port.
SERVER_PATTERN="${${(b)SERVER}//./\\.}"
running=$(pgrep -f "/Python +(-[A-Za-z]+ +)*${SERVER_PATTERN}\$")
if [[ -n "$running" ]]; then
    echo "העוזר הקולי כבר פועל (PID: ${running//$'\n'/, }). לא מופעל עותק נוסף."
    pause_and_exit 0
fi

listener=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null)
if [[ -n "$listener" ]]; then
    echo "שגיאה: פורט $PORT תפוס על ידי תהליך אחר. התהליך לא נסגר:"
    echo "$listener"
    pause_and_exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "שגיאה: סביבת Python לא נמצאה: $PYTHON (voice_env בהגדרות; ראו docs/configuration.md)"
    pause_and_exit 1
fi
for tool in "$CFG_CLAUDE" "$CFG_FFMPEG"; do
    if ! command -v "$tool" >/dev/null; then
        echo "שגיאה: הפקודה $tool לא נמצאה."
        pause_and_exit 1
    fi
done

echo "מפעיל את העוזר הקולי... (לעצירה: Ctrl+C)"
"$PYTHON" "$SERVER"
code=$?

# 130 = stopped with Ctrl+C, which is a normal way to end the server.
if [[ $code -ne 0 && $code -ne 130 ]]; then
    echo
    echo "השרת נעצר עם שגיאה (קוד $code). קרא את ההודעה למעלה."
    pause_and_exit "$code"
fi
echo "השרת נעצר."
