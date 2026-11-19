#!/usr/bin/env python3
"""The guided setup for the StickS3 voice assistant: one entry point over the existing tools (docs/getting-started.md).

    ./setup.sh                 the explanation and the plan (default): no download, no device connection, no restart, no persistent change
    ./setup.sh run             the guided setup: every consequential stage says what it will do and asks first
          [--port PORT]        the Stick's serial port (otherwise you choose it from the list when that stage comes)
          [--mac-option OPT]   pass an option to tools/setup_mac.py plan/apply (repeatable; e.g. --mac-option=--models-base=URL)
    ./setup.sh status          what is done, rechecked on this Mac (read-only; the Stick is not contacted)
    ./setup.sh forget          delete this wizard's own progress notes (asks first)

It never duplicates the tools it guides: the Mac is prepared by tools/setup_mac.py, the Stick is identified and backed up by
tools/stick_backup.py, its files are installed by tools/stick_install.py, and its key, auto-start and pairing are handled by
tools/stick_setup.py, each with its own checks. The wizard decides only the order, asks before each stage, and re-checks the real
state each time it runs (its notes are a convenience, never evidence).

It never asks for, reads or stores a password, Wi-Fi credentials or a key; never runs Claude or reads account data; never changes
global Claude settings or hooks; never installs system tools; never stops or kills a process; never flashes firmware.
Its progress notes (non-secret) live outside the project: ~/Library/Application Support/StickS3 Assistant/setup-wizard/.
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shlex  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

if sys.version_info < (3, 9):
    sys.exit("The setup needs Python 3.9 or newer (found %d.%d)." % sys.version_info[:2])

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
LAUNCHER = ROOT / "Start Voice Assistant.command"
SERVER_FILE = ROOT / "audio_server.py"
sys.path.insert(0, str(ROOT))
import stick_config  # noqa: E402

STOP, FAILED, DONE = "stop", "failed", "done"
SERVER_START_WAIT_S = 120

# ---- text (Hebrew for the person; the tools' own reports stay as they are) -----------------------------------------------------
INTRO = """\
התקנה מודרכת: העוזר הקולי ל-StickS3
==================================
מה צריך:
  • Mac עם שבב Apple silicon (נבדק רק על macOS 27.2), כ-4GB פנויים, וחיבור לאינטרנט להורדות (כ-3.4GB: סביבות Python ומודלים).
  • M5Stack StickS3 עם הקושחה UIFlow2 V2.4.8 (MicroPython 1.27.0, build 2026-06-26), כבל USB-C, ו-Wi-Fi שה-Mac וה-Stick מחוברים אליו.
  • חשבון Claude שיכול להשתמש ב-Claude Code (העוזר עונה דרכו). ההתחברות נעשית אצלכם; הכלי הזה לא רואה סיסמאות.
  • מותקנים מראש (הכלי רק בודק ומסביר איך): Python 3.14 ו-3.12, ffmpeg, Claude Code.

מגבלות נוכחיות (חשוב):
  • זו תצוגה מקדימה לשימוש לא-מסחרי בלבד: קול הדיבור העברי מורשה CC-BY-NC-4.0 (ראו THIRD_PARTY_NOTICES.md).
  • פרטיות ואבטחה: docs/privacy-he.md ו-docs/security.md. בגרסה הזאת העוזר לא קורא דואר או יומן.
  • התקנה על Stick חדש ועל Mac נקי לא נבדקה עדיין על חומרה אמיתית; היא נבדקה בסימולציה בלבד.
  • קושחה אחרת לא נתמכת, והכלי לעולם לא צורב קושחה.
"""

STAGES = [
    ("mac", "הכנת ה-Mac", "סביבות Python, מודלים וקובץ הגדרות (tools/setup_mac.py)"),
    ("claude", "התחברות ל-Claude", "אתם מתחברים; הכלי לא נוגע בהגדרות ולא רואה פרטי חשבון"),
    ("wifi", "Wi-Fi על ה-Stick", "דרך אפליקציית Setup של UIFlow, כשאף כלי לא מחובר ל-USB"),
    ("identify", "בחירה וזיהוי של ה-Stick", "יציאה מפורשת, דגם וקושחה נתמכים"),
    ("files", "גיבוי והתקנת הקבצים", "התוכנית המדויקת; לפני השינוי הראשון ב-Stick נלקח גיבוי פרטי ומאומת"),
    ("key", "מפתח המכשיר", "יצירה/העתקה בלי להציג אותו; מפתח קיים לא מוחלף"),
    ("server", "השרת", "מפעיל את השרת של התיקייה הזאת אם צריך; לא סוגר תהליכים"),
    ("autostart", "הפעלה אוטומטית", "boot_option 0, אחרי שכל השאר מוכן"),
    ("verify", "אימות", "הפעלה מחדש של ה-Stick ודיווח מאומת ראשון מהשרת"),
    ("voice", "בדיקה קולית", "אתם בודקים ומאשרים: הקלטה שקטה, שאלה ותשובה, כיבוי מסך"),
]
TITLES = {key: title for key, title, _ in STAGES}

WIFI_GUIDE = """\
הגדרת Wi-Fi נעשית על ה-Stick עצמו, באפליקציית Setup של UIFlow (לפי קוד המקור של UIFlow 2.4.8; לא נצפה בחומרה בערכה הזאת):
  1. ודאו שאף כלי לא מחובר ל-Stick ב-USB (הכלי הזה לא מחובר עכשיו). חשוב: בזמן השמירה הקושחה מדפיסה את הסיסמה לחיבור ה-USB.
  2. ב-Stick חדש מופיע תפריט ההפעלה של UIFlow. BtnB (הצד) = הבא, לחיצה על BtnA (החזית) = פתיחה, לחיצה כפולה = חזרה. בחרו Setup.
  3. ה-Stick פותח רשת פתוחה בשם S3_ ועוד ארבעה תווים. התחברו אליה מהטלפון, בחרו את הרשת שלכם והקלידו את הסיסמה בדף שנפתח.
  4. "Server OK" במסך הזה מתייחס לענן של M5Stack, לא ל-Mac שלכם.
אל תחזיקו את BtnA בזמן הדלקה של Stick שכבר הותקן: ב-UIFlow 2.4.8 זה מוחק את /flash/main.py.
"""


# ---- progress notes (non-secret, private, outside the project) ----------------------------------------------------------------
def progress_path():
    override = os.environ.get("STICK_WIZARD_STATE")
    if override:
        path = Path(override).expanduser()
    else:
        tag = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
        path = Path.home() / "Library" / "Application Support" / "StickS3 Assistant" / "setup-wizard" / f"progress-{tag}.json"
    resolved = path.resolve()
    if str(resolved).startswith(str(ROOT.resolve()) + os.sep):
        raise SystemExit("The wizard's progress notes must live outside the project folder (it may be a disposable copy). "
                         "Unset STICK_WIZARD_STATE or point it elsewhere.")
    return path


class Progress:
    """Notes about what happened, for resuming and for the summary. Never secrets, never treated as proof of the real state."""

    def __init__(self, write=True):
        self.path, self.write = progress_path(), write
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        if self.data.get("project") not in (None, str(ROOT)):
            self.data = {}
        self.data.setdefault("version", 1)
        self.data.setdefault("project", str(ROOT))
        self.data.setdefault("stages", {})
        self.session = uuid.uuid4().hex[:12]

    def note(self, stage, status, detail=""):
        self.data["stages"][stage] = {"status": status, "detail": detail, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.save()

    def set(self, **values):
        self.data.update(values)
        self.save()

    def save(self):
        if not self.write:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, self.path)


# ---- talking to the person --------------------------------------------------------------------------------------------------
class Stopped(Exception):
    pass


def say(text=""):
    print(text, flush=True)


def ask(question):
    """One yes/no answer from the keyboard. y/כ = yes, n/ל = no, q/ע = stop here (progress is kept). End of input also stops."""
    say("")
    prompt = f"{question} [כ/ל; ע=עצירה] » "
    while True:
        print(prompt, end="", flush=True)
        line = sys.stdin.readline()
        if not line:
            raise Stopped()
        answer = line.strip().lower()
        if answer in ("q", "ע", "עצירה", "stop"):
            raise Stopped()
        if answer in ("y", "yes", "כ", "כן"):
            return True
        if answer in ("n", "no", "ל", "לא"):
            return False
        say("נא לענות כ (כן), ל (לא) או ע (עצירה).")


def ask_text(question):
    say("")
    print(f"{question} (ע=עצירה) » ", end="", flush=True)
    line = sys.stdin.readline()
    if not line or line.strip().lower() in ("q", "ע", "עצירה", "stop"):
        raise Stopped()
    return line.strip()


def stage_header(key):
    index = [k for k, _, _ in STAGES].index(key) + 1
    say("")
    say(f"── שלב {index}/{len(STAGES)}: {TITLES[key]} " + "─" * 20)


# ---- running the existing tools ------------------------------------------------------------------------------------------------
def tool(name, *args, capture=False, timeout=None):
    """Run one of this project's tools with the same Python; its own report is shown as it is. (code, stdout if captured)."""
    command = [sys.executable, str(TOOLS / name), *args]
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE if capture else None, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, ""
    return done.returncode, done.stdout or ""


def tool_json(name, *args, timeout=None):
    code, out = tool(name, *args, capture=True, timeout=timeout)
    try:
        return code, json.loads(out)
    except ValueError:
        return code, None


def config():
    try:
        return stick_config.load(require_server=False)
    except stick_config.ConfigError:
        return None


# ---- the server: whose process owns the ports ------------------------------------------------------------------------------------
def listeners(port):
    """[(pid, command)] of processes listening on this TCP port (lsof; read-only)."""
    try:
        done = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for pid in sorted(set(done.stdout.split())):
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout.strip()
        found.append((pid, cmd))
    return found


def ours(command):
    """This folder's server: a Python process started with exactly this project's audio_server.py (as the launchers decide)."""
    parts = command.split()
    return bool(parts) and "python" in Path(parts[0]).name.lower() and command.endswith(" " + str(SERVER_FILE))


def panel_status(cfg):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg.panel_port}/api/status", timeout=3) as answer:
            data = json.loads(answer.read().decode())
        return data if isinstance(data, dict) and "key_provisioned" in data else None
    except (OSError, ValueError):
        return None


def server_state(cfg):
    """("free" | "ours" | "conflict", details) for the configured device and panel ports."""
    owners = {port: listeners(port) for port in (cfg.device_port, cfg.panel_port)}
    held = [(port, pid, cmd) for port, items in owners.items() for pid, cmd in items]
    if not held:
        return "free", []
    foreign = [(port, pid, cmd) for port, pid, cmd in held if not ours(cmd)]
    return ("conflict", foreign) if foreign else ("ours", held)


# ---- the stages -----------------------------------------------------------------------------------------------------------------
def missing_for(plan, step):
    """The missing prerequisites that block this Mac-setup step (a missing Python blocks the environment it would build)."""
    return [p for p in plan["prerequisites"] if p["status"] != "ok" and p.get("blocks") and p.get("needed_for") == step["id"]]


class Wizard:
    def __init__(self, args, progress):
        self.args, self.progress = args, progress
        self.port = args.port or progress.data.get("port")
        self.device = None
        self.backup = None
        self.mac_options = list(args.mac_option or [])

    # 1. the Mac -------------------------------------------------------------------------------------------------------------------
    def mac_plan(self):
        code, plan = tool_json("setup_mac.py", "plan", "--json", *self.mac_options, timeout=600)
        return plan

    def stage_mac(self):
        plan = self.mac_plan()
        if not plan:
            say("לא הצלחתי לקרוא את תוכנית הכנת ה-Mac (tools/setup_mac.py plan). הריצו אותו ישירות כדי לראות את ההודעה.")
            return FAILED
        if plan.get("platform_problem"):
            say("ה-Mac הזה לא נתמך: " + plan["platform_problem"])
            return FAILED
        missing = [p for p in plan["prerequisites"] if p["status"] != "ok"]
        todo = [s for s in plan["steps"] if s["status"] == "todo"]
        bad = [s for s in plan["steps"] if s["status"] == "conflict" or (s["status"] == "blocked" and not missing_for(plan, s))]
        if bad:
            say("יש בעיה שהכלי לא מתקן לבד:")
            for s in bad:
                say(f"  • {s['title']}: {s['detail']}")
            return FAILED
        blocking = [p for p in missing if p.get("blocks") and any(s["id"] == p.get("needed_for") for s in plan["steps"] if s["status"] in ("todo", "blocked"))]
        if missing:
            say("חסרים כלים שאתם מתקינים בעצמכם (הכלי הזה לא מריץ התקנות מערכת):")
            for p in missing:
                say(f"  • {p['title']}: {p['guide']}")
        if blocking or (not todo and missing):
            say("התקינו את מה שחסר והריצו שוב: ./setup.sh run")
            return STOP
        if not todo:
            say("ה-Mac מוכן: הסביבות, המודלים וקובץ ההגדרות קיימים ונבדקו. לא נעשה שינוי.")
            return DONE
        say("ההכנה תעשה את הצעדים האלה (tools/setup_mac.py):")
        for s in todo:
            say(f"  • {s['title']}")
        total = plan.get("needed_bytes", 0)
        say(f"הורדה של כ-{total / 1e9:.1f}GB (נבדקת ב-SHA-256), בלי sudo, בלי שינוי בהגדרות המערכת או ב-shell.")
        extra = []
        if any(s["id"] == "config" for s in todo):
            say("\nה-Stick צריך את הכתובת של ה-Mac ברשת ה-Wi-Fi. הכתובות של ה-Mac כרגע:")
            subprocess.run([sys.executable, str(ROOT / "stick_config.py"), "suggest-address"], stdin=subprocess.DEVNULL)
            address = ask_text("הקלידו את הכתובת שה-Stick ישתמש בה")
            extra = ["--advertise-host", address]
        if not ask("להתחיל את הכנת ה-Mac עכשיו?"):
            return STOP
        code, _ = tool("setup_mac.py", "apply", *extra, *self.mac_options)
        after = self.mac_plan()
        if code != 0 or not after or any(s["status"] == "todo" for s in after["steps"]):
            say("הכנת ה-Mac לא הסתיימה. ההודעה של הכלי מופיעה למעלה; הרצה חוזרת ממשיכה מאיפה שעצרה.")
            return FAILED
        if any(p["status"] != "ok" for p in after["prerequisites"]):
            say("הסביבות מוכנות, אבל עדיין חסרים כלים להפעלת העוזר (ראו למעלה). התקינו והריצו שוב.")
            return STOP
        return DONE

    # 2. Claude -------------------------------------------------------------------------------------------------------------------
    def stage_claude(self):
        if self.progress.data.get("stages", {}).get("claude", {}).get("status") == "user-confirmed":
            say("סימנתם קודם שהתחברתם ל-Claude Code (אישור שלכם; הכלי לא בודק חשבונות).")
            return DONE
        say("העוזר משתמש ב-Claude Code שלכם. ההתחברות נעשית אצלכם, בחלון Terminal נפרד:")
        say("  1. הריצו: claude")
        say("  2. בצעו את ההתחברות שהוא מציג (/login). הכלי הזה לא רואה סיסמה או חשבון, ולא משנה את ההגדרות הגלובליות שלכם.")
        say("  3. בתצוגה המקדימה העוזר משתמש רק בחיפוש באינטרנט: חיבורי דואר ויומן כבויים, גם אם הם פעילים בחשבון שלכם.")
        if not ask("התחברתם ל-Claude Code?"):
            say("אפשר להמשיך אחר כך: ./setup.sh run ימשיך מכאן.")
            return STOP
        self.progress.note("claude", "user-confirmed", "the person said they are logged in (not checked)")
        return DONE

    # 3. Wi-Fi --------------------------------------------------------------------------------------------------------------------
    def stage_wifi(self):
        saved = self.progress.data.get("wifi_saved_observed")
        if saved:
            say("בבדיקה הקודמת ה-Stick דיווח שיש Wi-Fi שמור (ייבדק שוב בזיהוי).")
            return DONE
        say(WIFI_GUIDE)
        held = [p for p in stick_config.usb_candidates(os.environ.get("STICK_DEV_DIR", "/dev")) if listeners_on_device(p)]
        if held:
            say("כרגע תוכנה כלשהי מחזיקה את יציאת ה-USB של ה-Stick: " + ", ".join(held) + ". סגרו אותה לפני שפותחים את Setup.")
            return STOP
        answer = ask("האם ה-Wi-Fi כבר שמור על ה-Stick (או ששמרתם אותו עכשיו)?")
        if not answer:
            say("שמרו Wi-Fi לפי ההוראות והריצו שוב: ./setup.sh run")
            return STOP
        self.progress.note("wifi", "user-confirmed", "the person said Wi-Fi is saved on the Stick (rechecked when it is identified)")
        return DONE

    # 4. which Stick ---------------------------------------------------------------------------------------------------------------
    def stage_identify(self):
        if not self.args.port:
            subprocess.run([sys.executable, str(TOOLS / "stick_backup.py"), "ports"], stdin=subprocess.DEVNULL)
            self.port = ask_text("הקלידו את היציאה של ה-Stick בדיוק כפי שמופיעה (למשל /dev/cu.usbmodem…)")
        say(f"היציאה: {self.port}. חיבור עוצר את האפליקציה שרצה על ה-Stick עד הפעלה מחדש; שום דבר לא נכתב.")
        if not ask("לזהות את ה-Stick עכשיו?"):
            return STOP
        code, ident = tool_json("stick_backup.py", "identify", "--port", self.port, "--json")
        if code != 0 or not ident:
            say("הזיהוי נכשל (ההודעה למעלה). בדקו את הכבל ואת היציאה, ושאף תוכנה אחרת לא מחזיקה אותה.")
            return FAILED
        if ident.get("support") != "supported":
            say("הקושחה ב-Stick הזה לא נתמכת (נדרש UIFlow2 V2.4.8). הכלי לא צורב קושחה ולא ממשיך.")
            return FAILED
        known = self.progress.data.get("device_id")
        if known and known != ident["device_id"] and not ask("זה Stick אחר מזה שהתחלתם איתו. להמשיך איתו?"):
            return STOP
        self.device = ident
        self.progress.set(port=self.port, device_id=ident["device_id"])
        wifi = ident.get("wifi_ssid_saved") is True and ident.get("wifi_password_saved") is True
        self.progress.set(wifi_saved_observed=wifi)
        if not wifi:
            say("ה-Stick דיווח שאין Wi-Fi שמור (שם ו/או סיסמה).")
            if ident.get("boot_option") == 0:
                say("ה-Stick מוגדר להפעלה אוטומטית, ולכן תפריט UIFlow לא יופיע.")
                return self.wifi_through_menu()
            say(self.restart_hint())
            say("הפעילו מחדש את ה-Stick, שמרו Wi-Fi לפי ההוראות בשלב 3, והריצו שוב.")
            self.progress.note("wifi", "needed", "not saved on the Stick")
            return STOP
        return DONE

    def restart_hint(self):
        cfg = config()
        program = cfg.mpremote if cfg else "mpremote"
        return f"להפעלה מחדש של ה-Stick: {shlex.quote(str(program))} connect {shlex.quote(self.port)} resume exec --no-follow \"import machine; machine.reset()\""

    # before the first change: one backup per run ----------------------------------------------------------------------------------
    def ensure_backup(self):
        """A fresh, complete backup of THIS Stick, once per run, taken only when something is about to change. True when ready."""
        if self.backup:
            return True
        say("לפני השינוי הראשון: גיבוי פרטי של כל קבצי ה-Stick (בלי מפתח המכשיר), עם בדיקה של כל קובץ מול ה-Stick. לוקח כדקה וחצי.")
        say("הגיבוי לא כולל את הגדרות ה-NVS (Wi-Fi, boot_option) ולא את הקושחה.")
        if not ask("לגבות עכשיו?"):
            raise Stopped()
        code, out = tool("stick_backup.py", "backup", "--port", self.port, "--expect-device", self.device["device_id"], capture=True)
        say(out)
        folder = next((line.split("Manifest: ", 1)[1].rsplit("/manifest.json", 1)[0] for line in out.splitlines() if line.startswith("Manifest: ")), None)
        if code != 0 or not folder or "Backup COMPLETE" not in out:
            say("הגיבוי לא הושלם, ולכן שום דבר ב-Stick לא ישתנה.")
            return False
        self.backup = folder
        self.progress.set(last_backup={"folder": folder, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        return True

    def wifi_through_menu(self):
        say("כדי להגיע לאפליקציית Setup, הכלי יכבה הפעלה אוטומטית (boot_option 1). כל הקבצים נשארים; ההפעלה תחזור בשלב ההפעלה האוטומטית.")
        if not ask("לכבות הפעלה אוטומטית עכשיו?"):
            return STOP
        if not self.ensure_backup():
            return FAILED
        code, _ = tool("stick_setup.py", "autostart", "--port", self.port, "--expect-device", self.device["device_id"], "--off", "--backup", self.backup)
        if code != 0:
            return FAILED
        say(self.restart_hint())
        say("הפעילו מחדש את ה-Stick, שמרו Wi-Fi לפי ההוראות בשלב 3, והריצו שוב: ./setup.sh run")
        return STOP

    # 6. files ---------------------------------------------------------------------------------------------------------------------
    def stage_files(self):
        code, found = tool_json("stick_install.py", "preflight", "--port", self.port, "--expect-device", self.device["device_id"], "--json")
        if not found or not found.get("ok"):
            say("הבדיקה לפני ההתקנה נכשלה (ההודעה למעלה).")
            return FAILED
        if found["complete"]:
            say("כל קבצי העוזר כבר מותקנים ותואמים. לא נכתב דבר.")
            return DONE
        if found["conflict"]:
            say("יש התנגשויות ב-Stick (ראו למעלה). הכלי לא מוחק ולא מזיז אותן; צריך לטפל בהן ידנית.")
            return FAILED
        if not found["enough_space"]:
            say("אין מספיק מקום פנוי ב-Stick להתקנה. שום קובץ לא נמחק כדי לפנות מקום.")
            return FAILED
        say("תוכנית ההתקנה המדויקת:")
        for path in found["replace"]:
            say(f"  • יוחלף: {path}")
        for path in found["create"]:
            say(f"  • ייווצר: {path}")
        say("שום קובץ אחר לא ישתנה ולא יימחק. ההתקנה מעתיקה לאזור זמני, בודקת, ורק אז מחליפה (לא אטומית).")
        if not ask("להתקין בדיוק את הקבצים האלה?"):
            return STOP
        if not self.ensure_backup():
            return FAILED
        changes = sum((["--expect-changes", p] for p in found["replace"] + found["create"]), [])
        code, _ = tool("stick_install.py", "apply", "--port", self.port, "--expect-device", self.device["device_id"], "--backup", self.backup, *changes)
        if code != 0:
            say("ההתקנה לא הסתיימה. אל תפעילו מחדש את ה-Stick אם הכלי אמר שזה לא בטוח; הרצה חוזרת של ./setup.sh run ממשיכה.")
            return FAILED
        return DONE

    # 7. key -----------------------------------------------------------------------------------------------------------------------
    def stage_key(self):
        code, _ = tool("stick_setup.py", "key", "--port", self.port, "--expect-device", self.device["device_id"], "--check")
        if code == 0:
            say("ל-Stick כבר יש את המפתח של ההתקנה הזאת (זה עדיין לא אימות; הוא בשלב 10).")
            return DONE
        code, facts = tool_json("stick_setup.py", "status", "--port", self.port, "--expect-device", self.device["device_id"], "--json")
        if not facts or not facts.get("ok"):
            return FAILED
        if facts["key_on_stick"] != "absent":
            say("ל-Stick יש מפתח שונה, או שלהתקנה הזאת אין מפתח בזמן שלמכשיר יש. הכלי לא מחליף מפתחות (ההסבר למעלה).")
            return FAILED
        say("הכלי ייצור מפתח להתקנה הזאת (אם אין עדיין) בתיקייה הפרטית, ויעתיק אותו ל-Stick בלי להציג אותו.")
        if not ask("להתקין את המפתח?"):
            return STOP
        if not self.ensure_backup():
            return FAILED
        code, _ = tool("stick_setup.py", "key", "--port", self.port, "--expect-device", self.device["device_id"], "--backup", self.backup)
        return DONE if code == 0 else FAILED

    # 8. server --------------------------------------------------------------------------------------------------------------------
    def stage_server(self):
        cfg = config()
        if not cfg:
            say("ההגדרות לא תקינות; הריצו את שלב הכנת ה-Mac.")
            return FAILED
        state, details = server_state(cfg)
        if state == "conflict":
            say("תהליך אחר תופס יציאה שהעוזר צריך. הכלי לא סוגר אותו:")
            for port, pid, cmd in details:
                say(f"  • יציאה {port}: PID {pid}: {cmd}")
            say("אם זה עוזר קולי מתיקייה אחרת, עצרו אותו בחלון שלו (Ctrl+C); אחרת שנו יציאות ב-config/host.conf. ואז הריצו שוב.")
            return STOP
        if state == "ours":
            status = panel_status(cfg)
            if status and status.get("key_provisioned"):
                say("השרת של התיקייה הזאת פועל וטען מפתח.")
                return DONE
            say("השרת של התיקייה הזאת פועל אבל לא טען את המפתח (הוא קורא אותו רק כשהוא עולה).")
            say("עצרו אותו בחלון שלו (Ctrl+C). הכלי לא סוגר תהליכים בעצמו.")
            if not ask("עצרתם את השרת?"):
                return STOP
            state, _ = server_state(cfg)
            if state != "free":
                say("השרת עדיין פועל. הריצו שוב אחרי שעצרתם אותו.")
                return STOP
        say(f"הכלי יפתח חלון Terminal שמריץ את השרת של התיקייה הזאת ({LAUNCHER.name}).")
        if not ask("להפעיל את השרת?"):
            return STOP
        subprocess.run(["open", "-a", "Terminal", str(LAUNCHER)], stdin=subprocess.DEVNULL)
        deadline = time.time() + float(os.environ.get("STICK_WIZARD_SERVER_WAIT_S", SERVER_START_WAIT_S))
        while time.time() < deadline:
            time.sleep(1)
            state, _ = server_state(cfg)
            status = panel_status(cfg) if state == "ours" else None
            if status:
                if status.get("key_provisioned"):
                    say("השרת עלה וטען מפתח.")
                    return DONE
                say("השרת עלה אבל לא טען מפתח. ודאו ששלב 7 הושלם.")
                return FAILED
            if state == "conflict":
                return self.stage_server()
        say("השרת לא עלה בזמן. בדקו את ההודעה בחלון ה-Terminal שלו.")
        return FAILED

    # 9. auto-start ----------------------------------------------------------------------------------------------------------------
    def stage_autostart(self):
        code, _ = tool("stick_setup.py", "autostart", "--port", self.port, "--expect-device", self.device["device_id"], "--check")
        if code == 0:
            return DONE
        say("הכלי ישנה ערך אחד ב-Stick, boot_option, ל-0 (העוזר עולה לבד בהדלקה), ישמור את הערך הקודם ויקרא אותו בחזרה.")
        if not ask("להפעיל הפעלה אוטומטית?"):
            return STOP
        if not self.ensure_backup():
            return FAILED
        code, _ = tool("stick_setup.py", "autostart", "--port", self.port, "--expect-device", self.device["device_id"], "--on", "--backup", self.backup)
        return DONE if code == 0 else FAILED

    # 10. verify -------------------------------------------------------------------------------------------------------------------
    def stage_verify(self):
        say("הכלי יפעיל מחדש את ה-Stick ויחכה לדיווח מאומת ראשון מהשרת, מהאתחול החדש.")
        if not ask("להפעיל מחדש ולאמת?"):
            return STOP
        code, out = tool("stick_setup.py", "verify", "--port", self.port, "--expect-device", self.device["device_id"], capture=True)
        say(out)
        if code != 0:
            return FAILED
        self.progress.set(operational={"verified": True, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                       "evidence": "a fresh authenticated report from a new boot after the restart (correlation, not device identity)",
                                       "autostart_observed": "OBSERVED" in out})
        return DONE

    # 11. the person's voice check ---------------------------------------------------------------------------------------------------
    def stage_voice(self):
        say("הבדיקה האחרונה היא שלכם (הכלי לא שומע ולא מקליט):")
        results = {}
        for key, text in (("silent", "לחצו להקלטה ואל תגידו כלום. הופיע 'No speech' או 'Try again'?"),
                          ("answer", "לחצו, שאלו שאלה קצרה וחכו. שמעתם תשובה מדוברת מה-Stick?"),
                          ("sleep", "השאירו אותו כדקה בלי לגעת. המסך התעמעם לבד?")):
            results[key] = ask(text)
        self.progress.set(voice_check={"user_confirmed": results, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        return DONE if all(results.values()) else FAILED

    # ---- the run --------------------------------------------------------------------------------------------------------------
    def run(self):
        say(INTRO)
        if not ask("להתחיל את ההתקנה המודרכת? (כל שלב משמעותי יבקש אישור)"):
            return 3
        device_stages = ("identify", "files", "key", "server", "autostart", "verify", "voice")
        for key, _, _ in STAGES:
            stage_header(key)
            if key in device_stages[1:] and self.device is None:
                return self.finish(FAILED, key)
            try:
                result = getattr(self, "stage_" + key)()
            except (Stopped, KeyboardInterrupt):
                self.progress.note(key, "stopped", "stopped by the person at a question")
                raise
            confirmed = self.progress.data["stages"].get(key, {}).get("status") == "user-confirmed"
            if not (result == DONE and confirmed):                     # a stage the person confirmed keeps that label
                self.progress.note(key, {DONE: "done", STOP: "stopped", FAILED: "failed"}[result])
            if result != DONE:
                return self.finish(result, key)
        return self.finish(DONE, None)

    def finish(self, result, key):
        say("")
        if result == DONE:
            op = self.progress.data.get("operational") or {}
            voice = (self.progress.data.get("voice_check") or {}).get("user_confirmed") or {}
            say("סיכום:")
            say("  • אימות תפעולי (נמדד): " + ("דיווח מאומת חדש התקבל אחרי הפעלה מחדש של ה-Stick שזוהה. זה מתאם בזמן, לא זיהוי ישיר של המכשיר."
                                              if op.get("verified") else "לא בוצע"))
            say("  • בדיקה קולית (אישור שלכם, לא נמדד): " + ", ".join(f"{k}={'כן' if v else 'לא'}" for k, v in voice.items()))
            say("העוזר מותקן אצלכם. זו תצוגה מקדימה לשימוש לא-מסחרי; המגבלות הידועות: docs/getting-started.md.")
            return 0
        if result == STOP:
            say(f"נעצר בשלב: {TITLES.get(key, key)}. ההתקדמות נשמרה; ./setup.sh run ממשיך ובודק מחדש את המצב האמיתי.")
            return 3
        say(f"השלב '{TITLES.get(key, key)}' נכשל. לא נמשיך לשלבים הבאים. ההודעה של הכלי למעלה; תקנו והריצו שוב ./setup.sh run.")
        return 1


def listeners_on_device(port):
    try:
        done = subprocess.run(["lsof", port], capture_output=True, text=True, timeout=10)
        return bool(done.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


# ---- plan / status / forget ------------------------------------------------------------------------------------------------------
def print_plan(progress, rechecked):
    say(INTRO)
    say("השלבים (./setup.sh run עובר עליהם לפי הסדר, ושואל לפני כל שלב משמעותי):")
    for index, (key, title, detail) in enumerate(STAGES, 1):
        note = progress.data.get("stages", {}).get(key)
        mark = {"done": "✓", "user-confirmed": "✓ (אישור שלכם)", "failed": "✗ נכשל", "stopped": "… נעצר", "needed": "… נדרש"}.get(note["status"], "") if note else ""
        say(f"  {index:2}. {title}: {detail}" + (f"   [{mark} בהרצה קודמת]" if mark else ""))
    if rechecked:
        say("")
        say("ה-Mac, נבדק עכשיו (קריאה בלבד):")
        for line in rechecked:
            say("  " + line)
    say("")
    say("זו הייתה סקירה בלבד: שום דבר לא הורד, לא חובר, לא הופעל מחדש ולא נשמר. להתחלה: ./setup.sh run")


def recheck_mac():
    code, plan = tool_json("setup_mac.py", "plan", "--json", timeout=600)
    if not plan:
        return ["לא הצלחתי לקרוא את מצב ה-Mac (tools/setup_mac.py plan)."]
    lines = [f"{'✓' if p['status'] == 'ok' else '✗'} {p['title']}" for p in plan["prerequisites"]]
    lines += [f"{'✓' if s['status'] in ('done', 'preserved') else '…'} {s['title']}" for s in plan["steps"]]
    cfg = config()
    if cfg and cfg.advertise_host:
        state, details = server_state(cfg)
        lines.append({"free": "… השרת לא פועל", "ours": "✓ השרת של התיקייה הזאת פועל",
                      "conflict": "✗ תהליך אחר תופס יציאה של העוזר"}[state])
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setup.sh", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="plan", choices=("plan", "run", "status", "forget"))
    parser.add_argument("--port", help="the Stick's serial port (never chosen automatically)")
    parser.add_argument("--mac-option", action="append", metavar="OPT", help="an option for tools/setup_mac.py plan/apply (repeatable)")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.command == "plan":
        print_plan(Progress(write=False), None)
        return 0
    if args.command == "status":
        print_plan(Progress(write=False), recheck_mac())
        return 0
    if args.command == "forget":
        path = progress_path()
        if not path.exists():
            say("אין הערות התקדמות למחוק.")
            return 0
        try:
            if ask(f"למחוק את הערות ההתקדמות ({path})? ההתקנה עצמה לא תשתנה."):
                path.unlink()
                say("נמחק.")
        except Stopped:
            pass
        return 0
    progress = Progress()
    try:
        return Wizard(args, progress).run()
    except (Stopped, KeyboardInterrupt):
        progress.save()
        say("\nנעצר לבקשתכם. ההתקדמות נשמרה; ./setup.sh run ממשיך ובודק מחדש את המצב האמיתי.")
        return 3


if __name__ == "__main__":
    sys.exit(main())
