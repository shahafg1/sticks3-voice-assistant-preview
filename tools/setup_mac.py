#!/usr/bin/env python3
"""Mac host setup for the StickS3 voice assistant: plan, apply, remove.

    python3 tools/setup_mac.py plan                      what would be done (read-only: writes nothing, runs nothing that changes anything)
    python3 tools/setup_mac.py apply --advertise-host IP  do it (creates environments, downloads and verifies models, writes config/host.conf if absent)
    python3 tools/setup_mac.py remove                    what removal would delete (read-only)
    python3 tools/setup_mac.py remove --apply            delete exactly what this tool created

It prepares the HOST only. Claude login and connectors, the macOS firewall, and the Stick are separate steps that it lists but never performs
(see docs/mac-setup.md). Needs only a Python 3.9+ standard library to run, so it works before any environment exists.
It never installs system tools, never uses sudo, never edits shell profiles, never touches Homebrew, global Python packages or caches, never starts
the server, never runs Claude, never opens a serial connection and never reads account data.
"""
import sys

sys.dont_write_bytecode = True        # `plan` must leave no trace, not even bytecode

import argparse
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit("This setup tool needs Python 3.9 or newer to run (found %s). See docs/mac-setup.md, 'Before you start'." % platform.python_version())

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stick_config  # noqa: E402

STATE_DIRNAME = ".stick-setup"
OWNED = ".stick-setup-owned.json"
PARTIAL = ".stick-setup-partial.json"
DEFAULT_MODELS_BASE = "https://huggingface.co"
CHUNK = 1024 * 1024
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_BUSY = 0, 1, 2, 3

# What system tools to tell the user about. Verified against the official pages on the date given in docs/mac-setup.md; the tool itself never runs them.
GUIDE = {
    "python": "Install it with Homebrew:  brew install python@{v}   (https://formulae.brew.sh/formula/python@{v}),\n"
              "      or with the macOS installer from https://www.python.org/downloads/macos/ . Then run this setup again.",
    "ffmpeg": "Install it with Homebrew:  brew install ffmpeg   (https://formulae.brew.sh/formula/ffmpeg).",
    "claude": "Install Claude Code from Anthropic's official instructions (https://code.claude.com/docs/en/setup): the native installer\n"
              "      curl -fsSL https://claude.ai/install.sh | bash     or     brew install --cask claude-code\n"
              "      (macOS 13 or newer; Node.js is NOT needed). Logging in is a separate step.",
    "homebrew": "Homebrew is optional. If you want it, follow https://brew.sh (the installer explains what it will change and asks first).",
}


class SetupError(Exception):
    """A step failed; str(error) says what happened and what to do."""


# ---------------------------------------------------------------------------------------------------------------------------------------------
# small helpers

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data):
    path = Path(path)
    temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temp), str(path))


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_of(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0


def say(text=""):
    print(text, flush=True)


def parse_lock(path):
    """[(normalized name, exact version or None for a URL requirement)] from a pip hash-lock file."""
    found = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
        if match:
            found.append((norm(match.group(1)), match.group(2)))
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s+@\s+\S+", line)
        if match:
            found.append((norm(match.group(1)), None))
    return found


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def safe_to_delete_tree(path, root):
    """Refuses anything that is not a plain directory strictly inside a location we could have created. Never a link, never a parent of the project."""
    p = Path(path)
    if not p.is_absolute() or p.is_symlink() or not p.is_dir():
        return False
    real, project, home = p.resolve(), Path(root).resolve(), Path.home().resolve()
    if real == Path(real.anchor) or real == home or real == project or real in project.parents or real in home.parents:
        return False
    return len(real.parts) >= 4


# ---------------------------------------------------------------------------------------------------------------------------------------------
# the installation being set up

class Setup:
    def __init__(self, root=ROOT, python_overrides=None, models_base=None, allow_untested_python=False, allow_untested_platform=False, out=say):
        self.root = Path(root)
        self.state_dir = self.root / STATE_DIRNAME
        self.state_file = self.state_dir / "state.json"
        self.lock_file = self.state_dir / "lock"
        self.out = out
        self.python_overrides = python_overrides or {}
        self.retry_delay = 2
        self.last_import_failure = []
        self.models_base = (models_base or os.environ.get("STICK_SETUP_MODELS_BASE") or DEFAULT_MODELS_BASE).rstrip("/")
        self.allow_untested_python = allow_untested_python
        self.allow_untested_platform = allow_untested_platform
        self.spec = read_json(self.root / "requirements" / "setup.json", {"environments": []})
        self.manifest = read_json(self.root / "requirements" / "models.json", {})
        self.state = read_json(self.state_file)
        self.config_error = None
        try:
            self.cfg = stick_config.load(self.root, require_server=False)
        except stick_config.ConfigError as error:
            self.cfg, self.config_error = None, str(error)

    # -- locations -------------------------------------------------------------------------------------------------------------------------
    def env_path(self, env):
        if self.cfg is not None:
            return Path(getattr(self.cfg, env["id"]))
        return self.root / stick_config.PATH_DEFAULTS[env["id"]]

    def model_groups(self):
        """[(group name, folder, repo, revision, [file dicts])] for the two pinned model sets."""
        groups = []
        for key, default_dir in (("tts", "models_dir"), ("whisper", "whisper_dir")):
            block = self.manifest.get(key)
            if not block:
                continue
            folder = Path(getattr(self.cfg, block.get("folder", default_dir))) if self.cfg is not None else self.root / stick_config.PATH_DEFAULTS[default_dir]
            groups.append((key, folder, block["repo"], block["revision"], block["files"]))
        return groups

    # -- host facts (read-only) ------------------------------------------------------------------------------------------------------------
    def host_facts(self):
        machine = platform.machine()
        rosetta = False
        if machine != "arm64":
            try:
                rosetta = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True, timeout=5).stdout.strip() == "1"
            except (OSError, subprocess.SubprocessError):
                pass
        usage = shutil.disk_usage(str(self.root))
        return {"system": platform.system(), "machine": machine, "rosetta": rosetta, "macos": platform.mac_ver()[0], "free_bytes": usage.free,
                "setup_python": platform.python_version()}

    def platform_problem(self):
        facts = self.host_facts()
        if facts["system"] != "Darwin":
            return "This setup is for macOS (found %s)." % facts["system"]
        if facts["machine"] != "arm64":
            return ("This terminal is running under Rosetta (Intel emulation); open a normal Terminal and run the setup again." if facts["rosetta"]
                    else "This setup is for Apple silicon Macs (found %s)." % facts["machine"])
        return None

    def find_python(self, version):
        """(path, 'x.y.z') of an interpreter of exactly this major.minor, or (None, why). The interpreter is only asked for its version."""
        wanted = self.python_overrides.get(version)
        candidates = [wanted] if wanted else []
        names = ["python" + version]
        extra = ["/opt/homebrew/bin", "/usr/local/bin", "/Library/Frameworks/Python.framework/Versions/%s/bin" % version]
        for name in names:
            found = shutil.which(name)
            if found:
                candidates.append(found)
            candidates += [os.path.join(d, name) for d in extra]
        seen = set()
        for candidate in candidates:
            if not candidate or candidate in seen or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
                continue
            seen.add(candidate)
            try:
                result = subprocess.run([candidate, "-c", "import platform,sys;print(platform.python_version());print(platform.machine())"],
                                        capture_output=True, text=True, timeout=15, env=clean_env())
            except (OSError, subprocess.SubprocessError):
                continue
            lines = result.stdout.split()
            if result.returncode == 0 and len(lines) == 2 and lines[0].startswith(version + ".") and lines[1] == "arm64":
                return candidate, lines[0]
        if wanted:
            return None, "the interpreter given for %s (%s) is not a usable arm64 Python %s" % (version, wanted, version)
        return None, "no Python %s found" % version

    def resolve_python(self, env):
        """(path, version, tested). The tested version first; with --allow-untested-python any other 3.10+ arm64 Python is accepted and labelled untested."""
        path, info = self.find_python(env["python"])
        if path:
            return path, info, True
        if self.allow_untested_python:
            for other in ("3.14", "3.13", "3.12", "3.11", "3.10"):
                if other != env["python"]:
                    path, found = self.find_python(other)
                    if path:
                        return path, found, False
        return None, info, False

    def find_tool(self, name, configured):
        if configured and "/" in configured:
            return configured if os.access(configured, os.X_OK) and os.path.isfile(configured) else None
        search = os.environ.get("PATH", "") + os.pathsep + "/opt/homebrew/bin" + os.pathsep + os.path.expanduser("~/.local/bin") + os.pathsep + os.path.expanduser("~/.npm-global/bin")
        return shutil.which(configured or name, path=search)

    # -- ownership and state ---------------------------------------------------------------------------------------------------------------
    def resource(self, key):
        return ((self.state or {}).get("resources") or {}).get(key)

    def owned_marker(self, path, key):
        """The marker inside a directory/venv we created, only if it carries THIS installation's id and the state agrees."""
        if not self.state:
            return None
        for name in (OWNED, PARTIAL):
            marker = read_json(Path(path) / name)
            if marker and marker.get("install_id") == self.state.get("install_id") and marker.get("key") == key:
                res = self.resource(key)
                if res and res.get("path") == str(path):
                    return {"complete": name == OWNED, "marker": marker}
        return None

    def save_state(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.state_dir), 0o700)
        write_json_atomic(self.state_file, self.state)

    def ensure_state(self):
        if not self.state:
            self.state = {"schema": 1, "install_id": uuid.uuid4().hex, "project": str(self.root), "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "resources": {}, "steps": {}}
            self.save_state()

    # -- inspection of the real state (never trusts the step markers alone) ----------------------------------------------------------------
    def installed_packages(self, env_path, smoke=()):
        """{normalized name: version} as the environment's own Python reports it, or None if it does not run. With `smoke`, the modules are also imported in a
        fresh process, and the result is None unless every one loads (a package whose files were damaged fails here even if its metadata is still present)."""
        python = Path(env_path) / "bin" / "python"
        code = ("import importlib.metadata as m,json,importlib\n"
                "bad=[]\n"
                "for name in %r:\n"
                "    try: importlib.import_module(name)\n"
                "    except Exception: bad.append(name)\n"
                "print(json.dumps([[[d.metadata['Name'],d.version] for d in m.distributions()], bad]))" % (list(smoke),))
        try:
            result = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=300, env=clean_env())
            if result.returncode != 0:
                return None
            packages, bad = json.loads(result.stdout.strip().splitlines()[-1])
            if bad:
                self.last_import_failure = bad
                return None
            return {norm(n): v for n, v in packages}
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None

    def env_status(self, env):
        """(status, detail). status: done | todo | preserved | blocked"""
        path = self.env_path(env)
        lock = self.root / env["lock"]
        if not lock.is_file():
            return "blocked", "the lock file %s is missing from this copy of the project" % env["lock"]
        if path.exists() or path.is_symlink():
            mine = self.owned_marker(path, env["id"])
            if mine is None:
                res = self.resource(env["id"])
                if res and res.get("status") == "creating" and res.get("absent_at_start") and path.is_dir() and not any(path.iterdir()):
                    return "todo", "an empty folder left by an interrupted setup (it will be reused)"
                return "preserved", "already exists and was not created by this setup: left unchanged and not verified (run tools/preflight.py to check it)"
            if not mine["complete"]:
                return "todo", "an incomplete environment from an interrupted setup (created by this setup: it will be rebuilt)"
            self.last_import_failure = []
            installed = self.installed_packages(path, env.get("smoke", ()))
            if installed is None:
                why = ("these modules no longer load: %s" % ", ".join(self.last_import_failure)) if self.last_import_failure else "its Python does not run"
                return "todo", "created by this setup but %s (it will be rebuilt)" % why
            wrong = [n for n, v in parse_lock(lock) if n not in installed or (v is not None and installed[n] != v)]
            if wrong:
                return "todo", "created by this setup but %d package(s) differ from the lock, e.g. %s (it will be rebuilt)" % (len(wrong), ", ".join(wrong[:3]))
            return "done", "complete: %d packages match the lock file" % len(parse_lock(lock))
        python, why, tested = self.resolve_python(env)
        if python is None:
            return "blocked", "needs Python %s: %s" % (env["python"], why)
        return "todo", "will be created with %s (Python %s%s)" % (python, why, "" if tested else ", NOT the tested version %s" % env["python"])

    def file_status(self, folder, name, entry):
        final = Path(folder) / name
        key = "file:" + str(final)
        if final.exists():
            res = self.resource(key)
            if final.is_file() and final.stat().st_size == entry["bytes"] and sha256_of(final) == entry["sha256"]:
                return "done", "present and verified" + ("" if res else " (it was already there: not created by this setup)")
            if res:
                return "todo", "created by this setup but does not verify (it will be downloaded again)"
            return "conflict", "a different file already exists at %s; it is not replaced (move it away and run setup again)" % final
        part = Path(folder) / ("." + name + ".stick-setup.part")
        if part.exists():
            return "todo", "partly downloaded (%s of %s): will continue" % (human(part.stat().st_size), human(entry["bytes"]))
        return "todo", "will be downloaded (%s)" % human(entry["bytes"])

    def config_status(self):
        path = self.root / stick_config.CONFIG_RELATIVE
        if path.exists():
            if self.config_error:
                return "blocked", "config/host.conf exists but is not valid: %s" % self.config_error.splitlines()[0]
            return "preserved", "config/host.conf already exists: kept exactly as it is"
        return "todo", "needs an explicit server address (--advertise-host); config/host.conf will be written once, never overwritten"

    # -- the plan ---------------------------------------------------------------------------------------------------------------------------
    def build_plan(self):
        facts = self.host_facts()
        plan = {"read_only": True, "project": str(self.root), "facts": facts, "platform_problem": self.platform_problem(), "prerequisites": [], "steps": [], "downloads": [], "notes": []}
        needed_bytes = 0
        for env in self.spec["environments"]:
            python, why, tested = self.resolve_python(env)
            plan["prerequisites"].append({"id": "python" + env["python"], "title": "Python %s (for the %s)" % (env["python"], env["title"].split(" (")[0].lower()),
                                          "status": "ok" if python else "missing", "detail": (python + " (Python " + why + ("" if tested else ", NOT the tested version") + ")") if python else why,
                                          "guide": "" if python else GUIDE["python"].format(v=env["python"]), "needed_for": env["id"], "blocks": True})
        for name, label, configured in (("ffmpeg", "ffmpeg", self.cfg.ffmpeg if self.cfg else None), ("claude", "Claude Code CLI", self.cfg.claude if self.cfg else None)):
            found = self.find_tool(name, configured)
            plan["prerequisites"].append({"id": name, "title": label, "status": "ok" if found else "missing", "detail": found or "not found",
                                          "guide": "" if found else GUIDE[name], "needed_for": "running the assistant (not needed to prepare the host)", "blocks": False})
        status, detail = self.config_status()
        plan["steps"].append({"id": "config", "title": "Host configuration (config/host.conf)", "status": status, "detail": detail, "path": str(self.root / stick_config.CONFIG_RELATIVE)})
        for env in self.spec["environments"]:
            status, detail = self.env_status(env)
            if status == "todo":
                needed_bytes += env.get("approx_bytes", 0)
                plan["downloads"].append({"what": env["title"], "from": "pypi.org / files.pythonhosted.org" + (" and github.com (phonikud archive)" if env["id"] == "tts_env" else ""),
                                          "approx_bytes": env.get("approx_bytes", 0), "verified_by": "SHA-256 of every package (pip --require-hashes)"})
            plan["steps"].append({"id": env["id"], "title": env["title"], "status": status, "detail": detail, "path": str(self.env_path(env))})
        for group, folder, repo, revision, files in self.model_groups():
            todo_bytes, statuses = 0, []
            for entry in files:
                status, detail = self.file_status(folder, entry["name"], entry)
                statuses.append((status, detail, entry))
                if status == "todo":
                    todo_bytes += entry["bytes"]
            worst = "conflict" if any(s == "conflict" for s, _, _ in statuses) else ("todo" if any(s == "todo" for s, _, _ in statuses) else "done")
            plan["steps"].append({"id": "models:" + group, "title": ("Hebrew speech-synthesis model files" if group == "tts" else "Speech-recognition model (Whisper)"),
                                  "status": worst, "detail": "; ".join("%s: %s" % (e["name"], d) for _, d, e in statuses), "path": str(folder)})
            if todo_bytes:
                needed_bytes += todo_bytes
                plan["downloads"].append({"what": group + " model files", "from": "%s (%s at revision %s)" % (self.models_base, repo, revision[:8]), "approx_bytes": todo_bytes,
                                          "verified_by": "size and SHA-256 from requirements/models.json"})
        plan["needed_bytes"] = needed_bytes
        plan["free_bytes"] = facts["free_bytes"]
        return plan

    # -- output ----------------------------------------------------------------------------------------------------------------------------
    def print_plan(self, plan, applying=False):
        say("Mac host setup: %s" % ("progress" if applying else "PLAN (read-only: nothing has been changed)"))
        say("Destination: %s" % plan["project"])
        facts = plan["facts"]
        say("This Mac: %s %s, macOS %s; free disk %s; setup runs on Python %s" % (facts["system"], facts["machine"], facts["macos"] or "?", human(facts["free_bytes"]), facts["setup_python"]))
        if plan["platform_problem"]:
            say("  PROBLEM: " + plan["platform_problem"])
        say("")
        say("Prerequisites (this tool never installs them):")
        for item in plan["prerequisites"]:
            say("  %-7s %s: %s" % ("ok" if item["status"] == "ok" else "MISSING", item["title"], item["detail"]))
            if item["guide"]:
                say("          How: " + item["guide"])
                say("          Needed for: " + item["needed_for"])
        say("")
        say("Steps:")
        marks = {"done": "done   ", "todo": "to do  ", "preserved": "kept   ", "blocked": "BLOCKED", "conflict": "CONFLICT"}
        for step in plan["steps"]:
            say("  %s %s" % (marks[step["status"]], step["title"]))
            say("          %s" % step["detail"])
            say("          at %s" % step["path"])
        say("")
        if plan["downloads"]:
            say("Downloads (about %s in total, free disk %s):" % (human(plan["needed_bytes"]), human(plan["free_bytes"])))
            for d in plan["downloads"]:
                say("  - %s: about %s from %s; checked by %s" % (d["what"], human(d["approx_bytes"]), d["from"], d["verified_by"]))
            say("  Packages come from the public Python package index; nothing is sent about you. Hugging Face model files need no account.")
            say("")
        say("Model licences: see THIRD_PARTY_NOTICES.md. The Hebrew voice files are CC-BY-NC-4.0: non-commercial use only.")
        say("  Downloading them is not a licence decision, and nothing here says this kit is ready for commercial use or for anyone else.")
        say("")
        say("What apply changes: only the folders listed above, config/host.conf if it does not exist, and %s (progress notes, no secrets)." % STATE_DIRNAME)
        say("What it never does: install system tools, use sudo, edit shell profiles or global Python packages, change Homebrew, start the server, run Claude,")
        say("  read account data, install hooks, change sleep or firewall settings, open a serial connection, or overwrite anything it did not create.")

    def print_remaining(self, plan):
        host_ok = all(s["status"] in ("done", "preserved") for s in plan["steps"])
        say("")
        say("=" * 78)
        say("HOST PREPARATION: " + ("complete" if host_ok else "NOT complete"))
        for s in plan["steps"]:
            if s["status"] not in ("done", "preserved"):
                say("  - still to do: %s (%s)" % (s["title"], s["detail"]))
        say("")
        say("THE ASSISTANT IS NOT READY YET. These steps are still yours (this tool does none of them):")
        missing = [p for p in plan["prerequisites"] if not p["blocks"] and p["status"] != "ok"]
        say("  1. System tools:" + (" " + ", ".join(p["title"] for p in missing) + " missing: see the 'How' lines above" if missing else " ffmpeg and Claude Code were found"))
        say("  2. Claude account: install is not login. Log in with Claude Code yourself, and connect Gmail and Google Calendar if you want them (optional).")
        say("  3. Network: the Mac and the Stick must be on the same Wi-Fi; allow Python to accept incoming connections if macOS asks; keep the Mac's address stable.")
        say("  4. The Stick: Wi-Fi, firmware/UIFlow, the Stick's files and its key. Run ./setup.sh run (docs/getting-started.md).")
        say("  5. Check: run  python3 tools/preflight.py  and then start the assistant yourself.")
        say("  6. Licences: see THIRD_PARTY_NOTICES.md (the Hebrew voice is non-commercial); this host setup makes no licence decision.")
        say("=" * 78)

    # -- running ---------------------------------------------------------------------------------------------------------------------------
    def acquire_lock(self):
        """One setup at a time. The lock is published atomically WITH its contents (hard link of a finished temporary file), so another process can never
        see a half-written lock and mistake it for a stale one."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.state_dir), 0o700)
        info = {"pid": os.getpid(), "host": socket.gethostname(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        temp = self.state_dir / ("lock.%d.tmp" % os.getpid())
        temp.write_text(json.dumps(info), encoding="utf-8")
        try:
            for _ in range(3):
                try:
                    os.link(str(temp), str(self.lock_file))
                    return
                except FileExistsError:
                    pass
                other = read_json(self.lock_file)
                if other is None:                                               # unreadable: being replaced right now, or damaged
                    try:
                        age = time.time() - self.lock_file.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if age < 30:
                        time.sleep(0.3)
                        continue
                pid = (other or {}).get("pid")
                if isinstance(pid, int) and alive(pid) and pid != os.getpid():
                    raise SetupError("Another setup is already running (process %s, started %s). Wait for it to finish; nothing was changed. "
                                     "(If you are sure nothing is running, delete %s and try again.)" % (pid, other.get("started", "?"), self.lock_file))
                if read_json(self.lock_file) == other:                         # still the same stale lock: take it over
                    try:
                        os.unlink(str(self.lock_file))
                    except FileNotFoundError:
                        pass
                    self.out("Found a lock from an earlier setup that is no longer running; continuing.")
            raise SetupError("Could not take the setup lock %s. Another setup may have just started; try again in a moment." % self.lock_file)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def release_lock(self):
        other = read_json(self.lock_file, {})
        if other.get("pid") == os.getpid():
            try:
                os.unlink(str(self.lock_file))
            except FileNotFoundError:
                pass

    def apply(self, advertise_host=None, choose=None):
        problem = self.platform_problem()
        if problem and not self.allow_untested_platform:
            say("Stopped before changing anything: " + problem)
            return EXIT_REFUSED
        if self.config_error and (self.root / stick_config.CONFIG_RELATIVE).exists():
            say("Stopped before changing anything: " + self.config_error.splitlines()[0])
            say("Fix config/host.conf (docs/configuration.md) and run setup again.")
            return EXIT_REFUSED
        plan = self.build_plan()
        blockers = self.blockers(plan)
        if blockers:
            self.print_plan(plan)
            say("")
            say("Stopped before changing anything. Fix these first:")
            for item in blockers:
                say("  - " + item)
            return EXIT_REFUSED
        # an explicit address is required before the first change
        has_config = (self.root / stick_config.CONFIG_RELATIVE).exists()
        if not has_config and not advertise_host:
            advertise_host = choose() if choose else None
            if not advertise_host:
                say("Stopped before changing anything: setup needs YOU to choose the address the Stick will connect to (this Mac's address on your Wi-Fi).")
                say("List candidates:  python3 stick_config.py suggest-address     then run:  python3 tools/setup_mac.py apply --advertise-host <address>")
                return EXIT_REFUSED
        if advertise_host and not has_config:
            try:                                                                 # validate the chosen address BEFORE anything is changed
                stick_config.load(self.root, env={"STICK_ADVERTISE_HOST": advertise_host})
            except stick_config.ConfigError as error:
                say("Stopped before changing anything: that address cannot be used. " + " ".join(l.strip() for l in str(error).splitlines()[1:4]))
                return EXIT_REFUSED
        try:
            self.acquire_lock()
        except SetupError as error:
            say(str(error))
            return EXIT_BUSY
        try:
            self.ensure_state()
            return self.run_steps(advertise_host)
        finally:
            self.release_lock()

    def blockers(self, plan):
        items = []
        for p in plan["prerequisites"]:
            if p["blocks"] and p["status"] != "ok":
                owner = [s for s in plan["steps"] if s["id"] == p["needed_for"]][0]
                if owner["status"] == "todo" or owner["status"] == "blocked":
                    items.append("%s is missing (%s). %s" % (p["title"], p["detail"], p["guide"].replace("\n      ", " ")))
        for s in plan["steps"]:
            if s["status"] == "conflict":
                items.append("%s: %s" % (s["title"], s["detail"]))
            if s["status"] == "blocked" and s["id"] == "config":
                items.append(s["detail"])
        if plan["needed_bytes"] and plan["free_bytes"] < plan["needed_bytes"]:
            items.append("Not enough free disk space: about %s needed for what is still to do, %s free. Free some space (the amount is the sum of the sizes listed above)." % (human(plan["needed_bytes"]), human(plan["free_bytes"])))
        return items

    def run_steps(self, advertise_host):
        total = 1 + len(self.spec["environments"]) + len(self.model_groups())
        number = 0
        failed = []

        def step(title, function):
            nonlocal number
            number += 1
            say("[%d/%d] %s" % (number, total, title))
            try:
                function()
            except SetupError as error:
                failed.append((title, str(error)))
                say("      FAILED: " + str(error))
                return False
            except KeyboardInterrupt:
                say("\nInterrupted. Run the same command again to continue: finished work is kept and checked again.")
                raise
            return True

        step("Host configuration", lambda: self.step_config(advertise_host))
        for env in self.spec["environments"]:
            step(env["title"], lambda env=env: self.step_env(env))
        for group, folder, repo, revision, files in self.model_groups():
            step("Model files: " + group, lambda group=group, folder=folder, repo=repo, revision=revision, files=files: self.step_models(group, folder, repo, revision, files))
        plan = self.build_plan()
        say("")
        say("Checking everything again from the real state (not from notes)...")
        self.print_final_checks(plan)
        self.print_remaining(plan)
        if failed:
            say("")
            say("Some steps failed. Fix the cause and run the same command again: finished steps are verified and skipped.")
            for title, why in failed:
                say("  - %s: %s" % (title, why.splitlines()[0]))
            return EXIT_FAILED
        return EXIT_OK if all(s["status"] in ("done", "preserved") for s in plan["steps"]) else EXIT_FAILED

    def print_final_checks(self, plan):
        for s in plan["steps"]:
            say("  %-9s %s" % ({"done": "ok", "preserved": "kept"}.get(s["status"], s["status"].upper()), s["title"]))

    # -- steps -----------------------------------------------------------------------------------------------------------------------------
    def step_config(self, advertise_host):
        path = self.root / stick_config.CONFIG_RELATIVE
        if path.exists():
            say("      config/host.conf exists: kept as it is.")
            return
        try:
            target = stick_config.init_file(self.root, advertise_host)
        except stick_config.ConfigError as error:
            raise SetupError(str(error).splitlines()[0])
        self.state["resources"]["config"] = {"kind": "config", "path": str(target), "preserve_on_remove": True}
        self.save_state()
        say("      wrote %s (address %s). It is yours: setup never changes it again." % (target, advertise_host))

    def step_env(self, env):
        status, detail = self.env_status(env)
        path = self.env_path(env)
        if status == "done":
            say("      already complete: " + detail)
            return
        if status == "preserved":
            say("      kept: " + detail)
            return
        if status == "blocked":
            raise SetupError(detail)
        python, version, tested = self.resolve_python(env)
        if python is None:
            raise SetupError("Python %s not found. %s" % (env["python"], GUIDE["python"].format(v=env["python"])))
        key = env["id"]
        res = self.resource(key)
        if path.exists():
            # only a folder this setup created (valid marker + state) or an empty leftover of its own interrupted creation may be replaced
            mine = self.owned_marker(path, key)
            if mine is not None:
                if not safe_to_delete_tree(path, self.root) or not (path / "pyvenv.cfg").is_file():
                    raise SetupError("Refusing to delete %s: it does not look like a virtual environment this setup created." % path)
                say("      removing the incomplete or outdated environment this setup created, then rebuilding it...")
                shutil.rmtree(str(path))
            elif res and res.get("status") == "creating" and res.get("absent_at_start") and path.is_dir() and not any(path.iterdir()):
                os.rmdir(str(path))
            else:
                raise SetupError("%s exists and was not created by this setup; it is left alone." % path)
        self.state["resources"][key] = {"kind": "env", "path": str(path), "status": "creating", "absent_at_start": True, "python": version, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.save_state()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(str(path))                                                    # atomic: fails if something appeared meanwhile
        marker = {"install_id": self.state["install_id"], "key": key, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        write_json_atomic(path / PARTIAL, marker)
        log = self.log_path(key)
        say("      creating the environment with Python %s (log: %s)" % (version, log))
        self.run_logged([python, "-m", "venv", str(path)], log, "creating the environment")
        env_python = str(path / "bin" / "python")
        say("      installing %d pinned packages, each checked against its SHA-256 (this can take several minutes)..." % len(parse_lock(self.root / env["lock"])))
        self.run_logged([env_python, "-m", "pip", "install", "--require-hashes", "--no-deps", "--no-cache-dir", "--disable-pip-version-check", "--no-input",
                         "-r", str(self.root / env["lock"])], log, "installing the packages")
        for module in env.get("smoke", []):
            self.run_logged([env_python, "-c", "import " + module], log, "loading %s" % module)
        wrong = [n for n, v in parse_lock(self.root / env["lock"]) if (self.installed_packages(path) or {}).get(n) is None]
        if wrong:
            raise SetupError("After installing, these packages are missing: %s" % ", ".join(wrong[:5]))
        write_json_atomic(path / OWNED, marker)
        os.unlink(str(path / PARTIAL))
        self.state["resources"][key]["status"] = "complete"
        self.save_state()
        say("      complete and verified.")

    def log_path(self, name):
        logs = self.state_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs / (name.replace(":", "_") + ".log")

    def run_logged(self, command, log, what):
        with open(str(log), "ab") as handle:
            handle.write(("\n$ " + " ".join(command) + "\n").encode())
            handle.flush()
            result = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, env=clean_env(), stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            tail = ""
            try:
                tail = "\n".join(Path(log).read_text(errors="replace").splitlines()[-6:])
            except OSError:
                pass
            raise SetupError("%s failed (exit %d). The last lines of %s:\n%s\nIf this looks like a network problem, check the connection and run setup again." % (what, result.returncode, log, tail))

    def step_models(self, group, folder, repo, revision, files):
        folder = Path(folder)
        for entry in files:
            name = entry["name"]
            final = folder / name
            key = "file:" + str(final)
            status, detail = self.file_status(folder, name, entry)
            if status == "done":
                say("      %s: already verified." % name)
                if not self.resource(key) and final.is_file():
                    pass                                                       # pre-existing and matching: left alone and not recorded as ours
                continue
            if status == "conflict":
                raise SetupError("%s: %s" % (name, detail))
            created_dir = not folder.exists()
            folder.mkdir(parents=True, exist_ok=True)
            if created_dir:
                self.state["resources"].setdefault("dir:" + str(folder), {"kind": "dir", "path": str(folder)})
                self.save_state()
            part = folder / ("." + name + ".stick-setup.part")
            url = "%s/%s/resolve/%s/%s" % (self.models_base, repo, revision, name)
            say("      %s (%s)" % (name, human(entry["bytes"])))
            self.download(url, part, entry["bytes"], entry["sha256"])
            os.replace(str(part), str(final))                                  # only a complete, verified file is activated
            self.state["resources"][key] = {"kind": "file", "path": str(final), "sha256": entry["sha256"], "bytes": entry["bytes"]}
            self.save_state()
            say("        verified and activated.")

    def download(self, url, part, size, sha):
        last = None
        for attempt in range(3):
            try:
                self.fetch(url, part, size)
                break
            except (urllib.error.URLError, OSError, ConnectionError, http.client.HTTPException) as error:
                last = error
                say("        connection problem (%s); retrying in %d s..." % (getattr(error, "reason", error) or type(error).__name__, self.retry_delay * (attempt + 1)))
                time.sleep(self.retry_delay * (attempt + 1))
        else:
            raise SetupError("Could not download %s: %s. What was received is kept in %s and the next run continues from it." % (url, last, part))
        actual = part.stat().st_size
        if actual != size or sha256_of(part) != sha:
            got = sha256_of(part)
            part.unlink()
            raise SetupError("The downloaded file does not match the pinned size/checksum (size %d, expected %d; SHA-256 %s..., expected %s...). It was discarded and NOT activated. "
                             "Run setup again; if it repeats, the file at the source has changed." % (actual, size, got[:12], sha[:12]))

    def fetch(self, url, part, size):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have == size:
            return
        request = urllib.request.Request(url, headers={"User-Agent": "stick-setup/1", **({"Range": "bytes=%d-" % have} if have else {})})
        with urllib.request.urlopen(request, timeout=30) as response:
            mode = "ab" if have and getattr(response, "status", 200) == 206 else "wb"
            received = have if mode == "ab" else 0
            declared = response.headers.get("Content-Length")
            got_now = 0
            last_pct = -1
            with open(str(part), mode) as handle:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    received += len(block)
                    got_now += len(block)
                    pct = int(received * 100 / size) if size else 100
                    if pct // 10 != last_pct // 10:
                        last_pct = pct
                        say("        %3d%%" % min(pct, 100))
                    if received > size:
                        break
        if declared is not None and got_now < int(declared) and received < size:
            raise ConnectionError("the connection closed after %d of %d bytes" % (received, size))   # resumable: what arrived is kept

    # -- removal ---------------------------------------------------------------------------------------------------------------------------
    def allowed_deletions(self):
        """The only places removal may ever touch, recomputed from the CURRENT configuration and the pinned manifests: environments at their configured
        locations, the manifest's own file names inside the configured model folders, and those folders themselves (only if empty). Notes that name anything
        else (stale, edited or forged) are never acted on."""
        envs = {env["id"]: str(self.env_path(env)) for env in self.spec["environments"]}
        files, dirs = set(), set()
        for _, folder, _, _, entries in self.model_groups():
            dirs.add(str(folder))
            files.update(str(Path(folder) / e["name"]) for e in entries)
        return envs, files, dirs

    def removal_plan(self):
        plan = {"delete": [], "keep": [], "problems": []}
        allowed_envs, allowed_files, allowed_dirs = self.allowed_deletions()
        if not self.state:
            plan["problems"].append("No record of a setup in %s: there is nothing this tool created, so there is nothing to remove." % self.root)
            return plan
        for key, res in sorted((self.state.get("resources") or {}).items()):
            kind, path = res.get("kind"), res.get("path")
            if kind == "config":
                plan["keep"].append("%s (your configuration: kept)" % path)
            elif kind == "env":
                p = Path(path)
                if not p.exists():
                    plan["keep"].append("%s (already gone)" % path)
                elif allowed_envs.get(key) != path:
                    plan["keep"].append("%s (not where this environment is configured now: NOT deleted)" % path)
                elif self.owned_marker(p, key) and safe_to_delete_tree(p, self.root) and (p / "pyvenv.cfg").is_file():
                    plan["delete"].append(("env", path, "virtual environment created by this setup (whole folder)"))
                else:
                    plan["keep"].append("%s (not verifiably created by this setup: NOT deleted)" % path)
            elif kind == "file":
                p = Path(path)
                if not p.exists():
                    plan["keep"].append("%s (already gone)" % path)
                elif path not in allowed_files:
                    plan["keep"].append("%s (not one of the pinned model files: NOT deleted)" % path)
                elif p.is_file() and not p.is_symlink() and sha256_of(p) == res.get("sha256"):
                    plan["delete"].append(("file", path, "model file downloaded by this setup (still identical to what was verified)"))
                else:
                    plan["keep"].append("%s (changed since setup: NOT deleted)" % path)
            elif kind == "dir":
                if path in allowed_dirs:
                    plan["delete"].append(("dir", path, "folder created by this setup (removed only if empty afterwards)"))
                else:
                    plan["keep"].append("%s (not a configured model folder: NOT deleted)" % path)
        plan["delete"].sort(key=lambda item: {"file": 0, "env": 1, "dir": 2}.get(item[0], 3))      # files before the folders that held them
        plan["delete"].append(("state", str(self.state_dir), "this tool's own notes and logs (no secrets)"))
        for item in ("secrets/ and the device key", "data/ (settings, reminders), recordings/", "backups", "the Hugging Face and pip caches", "Homebrew, Python, ffmpeg, Claude Code", "shell profiles, hooks, firewall and sleep settings"):
            plan["keep"].append(item + " (never touched)")
        return plan

    def print_removal(self, plan, applying):
        say("Removal %s" % ("(applying)" if applying else "PLAN (read-only: nothing is deleted)"))
        for problem in plan["problems"]:
            say("  " + problem)
        say("Would delete:" if not applying else "Deleting:")
        for kind, path, why in plan["delete"]:
            say("  - %s: %s" % (path, why))
        say("Kept:")
        for item in plan["keep"]:
            say("  - " + item)

    def remove(self, apply=False):
        plan = self.removal_plan()
        self.print_removal(plan, apply)
        if not apply or plan["problems"]:
            if not apply and not plan["problems"]:
                say("")
                say("To delete exactly the items above, run the same command with --apply.")
            return EXIT_OK if not plan["problems"] else EXIT_REFUSED
        try:
            self.acquire_lock()
        except SetupError as error:
            say(str(error))
            return EXIT_BUSY
        try:
            allowed_envs, allowed_files, allowed_dirs = self.allowed_deletions()
            for kind, path, _ in plan["delete"]:
                p = Path(path)
                if kind == "env":
                    key = [k for k, r in self.state["resources"].items() if r.get("path") == path][0]
                    if not (allowed_envs.get(key) == path and self.owned_marker(p, key) and safe_to_delete_tree(p, self.root)):
                        say("  skipped (checks failed at the moment of deletion): " + path)
                        continue
                    shutil.rmtree(str(p))
                elif kind == "file":
                    if path in allowed_files and p.is_file() and not p.is_symlink():
                        p.unlink()
                elif kind == "dir":
                    if path not in allowed_dirs:
                        continue
                    try:
                        os.rmdir(str(p))                                       # only if empty
                    except OSError:
                        say("  kept (not empty): " + path)
            self.release_lock()
            for name in ("state.json", "state.json.tmp", "lock"):
                try:
                    os.unlink(str(self.state_dir / name))
                except FileNotFoundError:
                    pass
            logs = self.state_dir / "logs"
            if logs.is_dir():
                for f in logs.iterdir():
                    if f.is_file() and f.suffix == ".log":
                        f.unlink()
                try:
                    logs.rmdir()
                except OSError:
                    pass
            try:
                self.state_dir.rmdir()
            except OSError:
                say("  kept (not empty): " + str(self.state_dir))
        finally:
            self.release_lock()
        say("Done. What this tool installed was removed; your configuration and everything else was left alone.")
        return EXIT_OK


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PIP_", "PYTHON")) or k == "PYTHONIOENCODING"}
    env.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1", "PIP_NO_INPUT": "1"})
    return env


def choose_address_interactively():
    if not sys.stdin.isatty():
        return None
    found = stick_config.local_ipv4_addresses()
    say("Which address should the Stick use to reach this Mac? (its address on your Wi-Fi)")
    for index, address in enumerate(found, 1):
        say("  %d) %s" % (index, address))
    say("Type the number, or the full address. There is no default: press Enter alone to stop.")
    answer = input("> ").strip()
    if not answer:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(found):
        return found[int(answer) - 1]
    return answer


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setup_mac.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--python", action="append", default=[], metavar="X.Y=PATH", help="use this interpreter for that Python version (repeatable)")
    common.add_argument("--allow-untested-python", action="store_true", help="try a different Python version than the one the project was tested with")
    common.add_argument("--allow-untested-platform", action="store_true", help="continue on a Mac that is not Apple silicon (not supported)")
    common.add_argument("--models-base", help="download model files from this base URL instead of huggingface.co (a mirror; files are still checked against the pinned hashes)")
    common.add_argument("--json", action="store_true", help="plan: print JSON")
    sub.add_parser("plan", parents=[common], help="show what would be done (read-only)")
    apply = sub.add_parser("apply", parents=[common], help="do it")
    apply.add_argument("--advertise-host", help="the address the Stick will use to reach this Mac (required unless config/host.conf exists)")
    remove = sub.add_parser("remove", parents=[common], help="show (or with --apply, perform) removal of what this tool created")
    remove.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    overrides = {}
    for item in args.python:
        version, _, path = item.partition("=")
        if not path:
            parser.error("--python needs X.Y=PATH")
        overrides[version] = path
    setup = Setup(python_overrides=overrides, models_base=args.models_base, allow_untested_python=args.allow_untested_python, allow_untested_platform=args.allow_untested_platform)
    try:
        if args.command == "plan":
            plan = setup.build_plan()
            if args.json:
                print(json.dumps(plan, indent=2, sort_keys=True))
            else:
                setup.print_plan(plan)
                setup.print_remaining(plan)
            return EXIT_OK
        if args.command == "apply":
            return setup.apply(args.advertise_host, choose=choose_address_interactively)
        return setup.remove(apply=args.apply)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
