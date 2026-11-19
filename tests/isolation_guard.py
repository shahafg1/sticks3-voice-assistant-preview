"""Test isolation guard: automated tests must never touch production resources.

Every test file starts with `import isolation_guard`, which installs an audit hook (sys.addaudithook) that raises
IsolationViolation the moment this process, or any Python child it starts, tries to
  * open, create, list, rename, delete or connect SQLite to anything under the project's data/, secrets/ or recordings/,
    ~/sticks3-project/{data,secrets,recordings}, ~/.claude (settings and hooks), or the Stick's own /flash and serial ports;
  * connect or bind a socket to the live service (ports 8000/8001 on any local address, including this Mac's LAN address), or reach any
    host other than loopback and this machine's own LAN address (no outside network);
  * start the real `claude` CLI (a paid AI call), mpremote, the desktop launcher or a sound player.
The hook runs BEFORE the operation, so a violation is blocked, not just reported. It derives from BaseException so a broad
`except Exception` in the code under test cannot swallow it, and it also appends a line to STICK_TEST_GUARD_LOG (when set) so a
runner can fail even if something catches it. Children started by the tests pick the guard up through tests/guard_site/sitecustomize.py
(PYTHONPATH). Hardware and manual checks do not belong in tests/: see tests/README.md.
Extra forbidden path prefixes can be added with STICK_TEST_FORBIDDEN (os.pathsep separated).
"""
import os
import re
import shutil
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GUARD_SITE = os.path.join(TESTS_DIR, "guard_site")
ROOT = os.path.dirname(TESTS_DIR)
LIVE_PORTS = {8000, 8001}
DOC_ADDRESS = "192.0.2.1"                              # RFC 5737 documentation address: the tests use it only to pick a route (no packet is sent)
LOCAL_HOSTS = {"", "0.0.0.0", "127.0.0.1", "localhost", "::1", "::"}
FORBIDDEN_COMMANDS = ("mpremote", "Start Voice Assistant", "launchctl", "afplay", "/usr/bin/say")
STICK_PATHS = ("/flash", "/dev/cu.", "/dev/tty.usb", "/dev/tty.SLAB", "/dev/tty.wch")


class IsolationViolation(BaseException):
    """A test reached for a production resource."""


def _own_lan_address():
    """This machine's LAN address (UDP connect sends nothing). Tests may talk to themselves there, e.g. to prove the panel is not reachable on it."""
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((DOC_ADDRESS, 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


_OWN_LAN = _own_lan_address()
# The live server's LAN address: this Mac's own, plus any listed in STICK_TEST_LIVE_HOSTS (comma-separated) for a Mac on several networks.
LIVE_HOSTS = {h for h in [_OWN_LAN, *os.environ.get("STICK_TEST_LIVE_HOSTS", "").split(",")] if h}


def _norm(path):
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return None


def forbidden_prefixes():
    home = os.path.expanduser("~")
    roots = {ROOT, os.path.join(home, "sticks3-project")}
    prefixes = []
    for base in roots:
        prefixes += [os.path.join(base, name) for name in ("data", "secrets", "recordings")]
    prefixes.append(os.path.join(home, ".claude"))
    prefixes += [p for p in os.environ.get("STICK_TEST_FORBIDDEN", "").split(os.pathsep) if p]
    return [_norm(p) for p in prefixes if p]


_FORBIDDEN = forbidden_prefixes()
_TMP_ROOTS = [_norm(tempfile.gettempdir()), _norm("/tmp"), _norm("/private/tmp"), _norm("/var/folders"), _norm("/private/var/folders")]


def _violation(what, target):
    message = f"isolation violation: {what} {target!r}"
    log = os.environ.get("STICK_TEST_GUARD_LOG")
    if log:
        try:
            with open(log, "a") as handle:
                handle.write(message + f" (pid {os.getpid()})\n")
        except OSError:
            pass
    print(message, file=sys.stderr, flush=True)
    raise IsolationViolation(message)


def check_path(path, what="path"):
    text = os.fspath(path) if isinstance(path, (str, os.PathLike)) else None
    if text is not None and not os.path.isabs(text) and os.sep not in text:
        return          # a bare name: relative to a directory fd (shutil.rmtree does this) or the cwd; no way to tell, and no separator to hide a path in
    normal = _norm(path)
    if normal is None:
        return
    for prefix in _FORBIDDEN:
        if normal == prefix or normal.startswith(prefix + os.sep):
            _violation(what, normal)
    for prefix in STICK_PATHS:
        if normal == prefix.rstrip("/") or normal.startswith(prefix):
            _violation(what, normal)


def check_address(address, what):
    if isinstance(address, str):                       # AF_UNIX
        check_path(address, what)
        return
    if not isinstance(address, tuple) or len(address) < 2:
        return
    host, port = str(address[0]), address[1]
    local = host in LOCAL_HOSTS or host in LIVE_HOSTS or host == _OWN_LAN
    if port in LIVE_PORTS and local:                   # the running server (8000) and its panel (8001), however they are addressed
        _violation(what, address[:2])
    if not (host in LOCAL_HOSTS or host == _OWN_LAN or host == DOC_ADDRESS):
        _violation(what + " (outside network)", address[:2])


# (ported from feature/mac-audio-output, without its Mac-audio code: a test-isolation rule only; the addresses come from LIVE_HOSTS/LIVE_PORTS)
_LIVE_ADDRESS = re.compile(r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]|::1|" + "|".join(re.escape(h) for h in sorted(LIVE_HOSTS)) + r"):(?:"
                           + "|".join(str(p) for p in sorted(LIVE_PORTS)) + r")(?!\d)")
_SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
_PORT_WORDS = re.compile("|".join(str(p) for p in sorted(LIVE_PORTS)) + r"|PANEL_PORT|DEVICE_PORT")


def _script_knows_ports(path):
    """True if this file is a shell script that mentions the service's ports (a launcher, a probe): those must be given explicit test ports."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 262144:
            return False
        with open(path, "r", errors="replace") as handle:
            return bool(_PORT_WORDS.search(handle.read()))
    except OSError:
        return False


def _explicit_test_ports(env):
    """True if env (None means this process's own) names both ports explicitly and neither is a live one."""
    env = os.environ if env is None else env
    try:
        ports = [int(str(env.get(name, "")).strip()) for name in ("STICK_DEVICE_PORT", "STICK_PANEL_PORT")]
    except ValueError:
        return False
    return all(0 < port < 65536 and port not in LIVE_PORTS for port in ports) and ports[0] != ports[1]


def check_command(executable, args, what="subprocess", env=None):
    parts = [str(executable or "")] + [str(a) for a in (args if isinstance(args, (list, tuple)) else [args])]
    # A child process addressed at the running assistant (curl, a launcher's own probe...) is as forbidden as a socket from this process.
    for part in parts:
        if _LIVE_ADDRESS.search(part):
            _violation(what + " aimed at the live service", parts[:3])
    # A shell script (a desktop launcher, a tool) falls back to the DEFAULT ports, i.e. the running assistant, unless the test names its own ports.
    # Syntax checks (-n) and inline commands (-c) are not treated as running a script file.
    if os.path.basename(parts[0]) in _SHELLS and "-n" not in parts[2:4] and "-c" not in parts[2:4]:      # parts[1] is argv[0]
        script = next((p for p in parts[2:] if not p.startswith("-")), None)
        if script and _script_knows_ports(script) and not _explicit_test_ports(env):
            _violation(what + " script started without explicit test ports (STICK_DEVICE_PORT, STICK_PANEL_PORT)", parts[:3])
    for part in parts:
        if any(marker in part for marker in FORBIDDEN_COMMANDS) or any(s in part for s in ("/dev/cu.", "/dev/tty.usb")):
            _violation(what, parts[:3])
    name = os.path.basename(parts[0])
    if name == "claude":                               # the real CLI would make a paid AI call: only a test stub is allowed
        found = shutil.which(parts[0]) if os.sep not in parts[0] else parts[0]
        resolved = _norm(found) if found else None
        if resolved is None or not any(resolved.startswith(root + os.sep) for root in _TMP_ROOTS if root):
            _violation("real claude CLI", found or parts[0])


_PATH_EVENTS = {"open": (0,), "os.mkdir": (0,), "os.rmdir": (0,), "os.remove": (0,), "os.rename": (0, 1), "os.listdir": (0,),
                "os.scandir": (0,), "os.chmod": (0,), "os.chown": (0,), "os.truncate": (0,), "os.utime": (0,),
                "os.symlink": (0, 1), "os.link": (0, 1), "shutil.copyfile": (0, 1), "shutil.copytree": (0, 1),
                "shutil.rmtree": (0,), "shutil.move": (0, 1), "sqlite3.connect": (0,), "os.chdir": (0,),
                "os.walk": (0,), "glob.glob": (0,)}


def _hook(event, args):
    try:
        if event in _PATH_EVENTS:
            for index in _PATH_EVENTS[event]:
                if index < len(args) and isinstance(args[index], (str, bytes, os.PathLike)):
                    value = args[index]
                    check_path(value.decode(errors="replace") if isinstance(value, bytes) else value, event)
        elif event in ("socket.connect", "socket.bind", "socket.sendto") and len(args) >= 2:
            check_address(args[1], event)
        elif event == "subprocess.Popen" and len(args) >= 2:
            check_command(args[0], args[1], env=args[3] if len(args) > 3 else None)
        elif event == "os.system" and args:
            check_command("sh", [args[0]], "os.system")
    except IsolationViolation:
        raise
    except Exception:                                  # noqa: BLE001 - a bug in the guard must never break an unrelated operation
        pass


_installed = False


def install():
    """Install the hook (once per process) and make Python children load it too. Safe to call repeatedly."""
    global _installed
    if _installed:
        return
    _installed = True
    sys.addaudithook(_hook)
    os.environ["STICK_TEST_GUARD"] = "1"
    paths = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if GUARD_SITE not in paths:
        os.environ["PYTHONPATH"] = os.pathsep.join(paths + [GUARD_SITE])


def child_pythonpath(*entries):
    """PYTHONPATH for a child whose environment a harness builds from scratch: its own entries, then the guard."""
    return os.pathsep.join([*entries, GUARD_SITE])


install()
