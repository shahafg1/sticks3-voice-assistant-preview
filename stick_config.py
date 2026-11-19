#!/usr/bin/env python3
"""Host configuration for the Mac side of the assistant (documented in docs/configuration.md).

One small file, `config/host.conf` (INI format, comments allowed, never committed), says where this installation lives and which address the Stick uses
to reach it. Everything else is derived from the folder this file is installed in, so the project can live anywhere (spaces are fine).

Precedence for every value: environment variable > config file > built-in default. There is deliberately NO default for `advertise_host`: a missing
address produces an actionable message instead of a guess. Standard library only and Python 3.9 compatible, so launchers, hooks and the preflight can
use it before any environment exists (the hook runs it under `python -I -S`).

Command line (all read-only except `init` and `device-config --out`):
    python3 stick_config.py show [--json]          effective values and where each came from
    python3 stick_config.py validate               exit 0 if usable, 2 with a message if not
    python3 stick_config.py shell                  CFG_* variables for the launchers and tools (shell-quoted)
    python3 stick_config.py device-config [--out FILE]   the device's own configuration (server address and port only)
    python3 stick_config.py init --advertise-host ADDRESS [--keep-existing-locations]   write config/host.conf (never overwrites)
    python3 stick_config.py suggest-address        this Mac's IPv4 addresses (read-only; you choose)
"""
from __future__ import annotations

import configparser
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent          # derived from where this file is installed, never from the current folder
CONFIG_RELATIVE = Path("config") / "host.conf"
DEFAULT_DEVICE_PORT = 8000
DEFAULT_PANEL_PORT = 8001
DEVICE_CONFIG_VERSION = 1

# key -> (section, environment variable)
KEYS = {
    "advertise_host": ("server", "STICK_ADVERTISE_HOST"),
    "bind_host": ("server", "STICK_BIND_HOST"),
    "device_port": ("server", "STICK_DEVICE_PORT"),
    "panel_port": ("server", "STICK_PANEL_PORT"),
    "usb_port": ("device", "STICK_PORT"),
    "mpremote": ("paths", "MPREMOTE"),
    "claude": ("paths", "STICK_CLAUDE"),
    "ffmpeg": ("paths", "STICK_FFMPEG"),
    "voice_env": ("paths", "STICK_VOICE_ENV"),
    "tts_env": ("paths", "STICK_TTS_ENV"),
    "tools_env": ("paths", "STICK_TOOLS_ENV"),
    "models_dir": ("paths", "STICK_MODELS_DIR"),
    "whisper_dir": ("paths", "STICK_WHISPER_DIR"),
    "data_dir": ("paths", "STICK_DATA_DIR"),
    "secrets_dir": ("paths", "STICK_SECRETS_DIR"),
    "recordings_dir": ("paths", "STICK_RECORDINGS_DIR"),
}
PATH_DEFAULTS = {   # relative to the project folder (the layout the project has always used)
    "voice_env": "voice-env", "tts_env": "tts-env312", "tools_env": "tools-env", "models_dir": "tts-models", "whisper_dir": "whisper-model",
    "data_dir": "data", "secrets_dir": "secrets", "recordings_dir": "recordings",
}
HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
USB_PATH = re.compile(r"^/dev/(cu|tty)\.[A-Za-z0-9._-]+$")
EXAMPLE = "config/host.example.conf"
USB_PREFIX = "cu.usbmodem"        # the name ESP32-S3 boards get on macOS
TOOL_PROGRAM = "mpremote"         # the Stick tool program (installed in tools_env/bin)


class ConfigError(Exception):
    """The configuration is missing or invalid. str(error) is a complete, actionable message."""


def config_file_path(root=None, env=None):
    """The configuration file to use: STICK_CONFIG (must exist), else <project>/config/host.conf (None if it does not exist)."""
    root = Path(root) if root else PROJECT_ROOT
    env = os.environ if env is None else env
    explicit = env.get("STICK_CONFIG")
    if explicit:
        path = Path(os.path.expanduser(explicit))
        if not path.is_file():
            raise ConfigError(f"STICK_CONFIG points to a file that does not exist: {path}")
        return path
    path = root / CONFIG_RELATIVE
    return path if path.is_file() else None


def _read_file(path):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error
    values, problems = {}, []
    known = {}
    for key, (section, _) in KEYS.items():
        known.setdefault(section, set()).add(key)
    for section in parser.sections():
        if section not in known:
            problems.append(f"unknown section [{section}] in {path} (expected: {', '.join(sorted(known))})")
            continue
        for key, value in parser.items(section):
            if key not in known[section]:
                problems.append(f"unknown setting '{key}' in [{section}] of {path} (expected: {', '.join(sorted(known[section]))})")
            elif value.strip():
                values[key] = value.strip()
    if problems:
        raise ConfigError("\n".join(problems))
    return values


class HostConfig:
    """Effective configuration. `sources[key]` is "env", "file" or "default" (or "derived")."""

    def __init__(self, root, config_file, values, sources):
        self.project_root = Path(root)
        self.config_file = config_file
        self.sources = sources
        self.advertise_host = values.get("advertise_host")
        self.bind_host = values.get("bind_host")
        self.device_port = values["device_port"]
        self.panel_port = values["panel_port"]
        self.usb_port = values.get("usb_port")
        self.claude = values.get("claude")
        self.ffmpeg = values.get("ffmpeg")
        self.voice_env = values["voice_env"]
        self.tts_env = values["tts_env"]
        self.tools_env = values["tools_env"]
        self.models_dir = values["models_dir"]
        self.whisper_dir = values["whisper_dir"]
        self.data_dir = values["data_dir"]
        self.secrets_dir = values["secrets_dir"]
        self.recordings_dir = values["recordings_dir"]
        self.mpremote = values.get("mpremote") or str(self.tools_env / "bin" / TOOL_PROGRAM)

    @property
    def voice_python(self):
        return self.voice_env / "bin" / "python"

    @property
    def tts_python(self):
        return self.tts_env / "bin" / "python"

    @property
    def connect_host(self):
        """The address a program on THIS Mac (the notification hook) uses to reach the server: loopback when it listens on every interface."""
        if self.bind_host in (None, "0.0.0.0"):
            return "127.0.0.1"
        return self.bind_host

    @property
    def notify_url(self):
        """The Claude Code hook's endpoint: the loopback-only panel listener (never the LAN device port; docs/security.md)."""
        return f"http://127.0.0.1:{self.panel_port}/hook/notify"

    def as_dict(self):
        out = {}
        for key in KEYS:
            value = getattr(self, key)
            out[key] = None if value is None else str(value)
        out["project_root"] = str(self.project_root)
        out["config_file"] = None if self.config_file is None else str(self.config_file)
        out["connect_host"] = self.connect_host if self.advertise_host else None
        return out


def _resolve_path(value, root):
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else (root / path)


def _port(name, text, problems):
    try:
        number = int(str(text), 10)
    except ValueError:
        problems.append(f"{name} must be a whole number (got {text!r})")
        return None
    if not 1024 <= number <= 65535:
        problems.append(f"{name} must be between 1024 and 65535 (got {number})")
        return None
    return number


def _ipv4(text):
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        return None


def load(root=None, env=None, require_server=True, config_path=None):
    """Read, merge and validate the configuration. Raises ConfigError with an actionable message.

    require_server=False is for tools that only need locations (they do not need to know the Stick's address)."""
    root = Path(root) if root else PROJECT_ROOT
    env = dict(os.environ if env is None else env)
    path = Path(config_path) if config_path else config_file_path(root, env)
    file_values = _read_file(path) if path else {}
    merged, sources = {}, {}
    for key, (_, env_name) in KEYS.items():
        if env.get(env_name, "").strip():
            merged[key], sources[key] = env[env_name].strip(), "env"
        elif key in file_values:
            merged[key], sources[key] = file_values[key], "file"
        else:
            sources[key] = "default"
    problems = []

    advertise = merged.get("advertise_host")
    if advertise is None:
        if require_server:
            problems.append("advertise_host is not set: the address the Stick uses to reach this Mac")
    else:
        ip = _ipv4(advertise)
        if ip is not None:
            if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
                problems.append(f"advertise_host {advertise} cannot be used: the Stick must reach this Mac on its LAN address, not a loopback or wildcard address")
        elif advertise.lower() == "localhost" or not HOSTNAME.match(advertise):
            problems.append(f"advertise_host {advertise!r} is neither an IPv4 address nor a valid host name")

    bind = merged.get("bind_host")
    if bind is None and advertise is not None and _ipv4(advertise) is not None and not problems:
        bind, sources["bind_host"] = advertise, "derived"       # listen only on the advertised address, as the project always did
    elif bind is None and advertise is not None and _ipv4(advertise) is None:
        problems.append("bind_host must be set when advertise_host is a host name (use this Mac's IPv4 address, or 0.0.0.0 to listen on every interface)")
    elif bind is not None and _ipv4(bind) is None:
        problems.append(f"bind_host {bind!r} must be an IPv4 address (or 0.0.0.0)")

    device_port = _port("device_port", merged.get("device_port", DEFAULT_DEVICE_PORT), problems)
    panel_port = _port("panel_port", merged.get("panel_port", DEFAULT_PANEL_PORT), problems)
    if device_port is not None and device_port == panel_port:
        problems.append("device_port and panel_port must differ")

    usb = merged.get("usb_port")
    if usb and not USB_PATH.match(usb):
        problems.append(f"usb_port {usb!r} must look like /dev/cu.usbmodemXXXX (leave it empty to auto-select when exactly one device is connected)")

    if problems:
        where = f" (read from {path})" if path else ""
        raise ConfigError(_fix_message(root, problems, path, where))

    values = {"advertise_host": advertise, "bind_host": bind, "device_port": device_port, "panel_port": panel_port, "usb_port": usb,
              "claude": merged.get("claude"), "ffmpeg": merged.get("ffmpeg")}
    for key, default in PATH_DEFAULTS.items():
        values[key] = _resolve_path(merged.get(key, default), root)
    if merged.get("mpremote"):
        values["mpremote"] = str(_resolve_path(merged["mpremote"], root))
    for key in ("claude", "ffmpeg"):                 # a bare command name is looked up on PATH; anything with a slash is a location
        if values[key] and "/" in values[key]:
            values[key] = str(_resolve_path(values[key], root))
    return HostConfig(root, path, values, sources)


def _fix_message(root, problems, path, where=""):
    lines = ["The host configuration is not usable" + where + ":"] + [f"  - {p}" for p in problems]
    if not path:
        lines += ["",
                  "No configuration file was found. To set this installation up:",
                  f"  1. Copy {root / EXAMPLE} to {root / CONFIG_RELATIVE}",
                  "  2. Set advertise_host to this Mac's address on the Wi-Fi network the Stick uses",
                  "     (to list this Mac's addresses: python3 stick_config.py suggest-address)",
                  "Existing installation that used the built-in address: run",
                  "  python3 stick_config.py init --advertise-host <the address your Stick already uses> [--keep-existing-locations]",
                  "See docs/configuration.md."]
    else:
        lines += ["", f"Edit {path} (see {EXAMPLE} and docs/configuration.md)."]
    return "\n".join(lines)


def usb_candidates(dev="/dev"):
    """Names of connected USB serial devices that look like an ESP32-S3 board. Lists /dev only: nothing is opened."""
    try:
        return sorted(f"{dev}/{n}" for n in os.listdir(dev) if n.startswith(USB_PREFIX))
    except OSError:
        return []


def resolve_usb_port(cfg, dev="/dev"):
    """(port, note). The configured/env value wins; otherwise the single connected candidate; otherwise ('', why)."""
    if cfg.usb_port:
        return cfg.usb_port, ""
    found = usb_candidates(dev)
    if len(found) == 1:
        return found[0], "auto-selected: the only USB serial device present"
    if not found:
        return "", "no USB serial device found: connect the Stick, or set usb_port in the configuration"
    return "", "several USB serial devices found (" + ", ".join(found) + "): set usb_port in the configuration"


def device_config(cfg):
    """What the Stick needs, and nothing else: no host paths, no Wi-Fi credentials, no authentication secret."""
    if not cfg.advertise_host:
        raise ConfigError(_fix_message(cfg.project_root, ["advertise_host is not set"], cfg.config_file))
    return {"v": DEVICE_CONFIG_VERSION, "server_host": cfg.advertise_host, "server_port": cfg.device_port}


def local_ipv4_addresses():
    """This Mac's IPv4 addresses (read-only: parses `ifconfig`, falls back to the routing table's choice)."""
    found = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        found = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)\b", out)
    except (OSError, subprocess.SubprocessError):
        pass
    if not found:
        import socket
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))              # documentation address: selects a route, sends nothing
            found = [probe.getsockname()[0]]
        except OSError:
            pass
        finally:
            probe.close()
    return [a for a in found if not a.startswith("127.")]


TEMPLATE = """# Host configuration for this installation (written by `stick_config.py init`; see config/host.example.conf and docs/configuration.md).
# This file is private to this Mac and is never committed.
[server]
advertise_host = {advertise}
{extra}"""


def init_file(root, advertise_host, keep_existing_locations=False, home=None):
    root = Path(root)
    target = root / CONFIG_RELATIVE
    if target.exists():
        raise ConfigError(f"{target} already exists; it was not changed. Edit it by hand, or remove it first.")
    extra = ""
    if keep_existing_locations:
        legacy = Path(home or os.path.expanduser("~")) / "sticks3-env"
        if (legacy / "bin" / TOOL_PROGRAM).exists():
            extra = f"\n[paths]\n# the Stick tools environment this installation already uses\ntools_env = {legacy}\n"
    text = TEMPLATE.format(advertise=advertise_host, extra=extra)
    import tempfile                                      # validate in a scratch folder first, so a typo never leaves a broken file behind
    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / "host.conf"
        probe.write_text(text, encoding="utf-8")
        load(root, env={}, config_path=probe)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def shell_lines(cfg):
    """Shell-quoted assignments for the launchers and tools. CFG_* names so nothing leaks into child environments by accident."""
    port, note = resolve_usb_port(cfg)
    pairs = {
        "CFG_PROJECT": cfg.project_root, "CFG_CONFIG_FILE": cfg.config_file or "", "CFG_ADVERTISE_HOST": cfg.advertise_host or "",
        "CFG_BIND_HOST": cfg.bind_host or "", "CFG_CONNECT_HOST": cfg.connect_host if cfg.advertise_host else "",
        "CFG_DEVICE_PORT": cfg.device_port, "CFG_PANEL_PORT": cfg.panel_port, "CFG_USB_PORT": port, "CFG_USB_NOTE": note,
        "CFG_MPREMOTE": cfg.mpremote, "CFG_VOICE_PYTHON": cfg.voice_python, "CFG_TTS_PYTHON": cfg.tts_python,
        "CFG_CLAUDE": cfg.claude or "claude", "CFG_FFMPEG": cfg.ffmpeg or "ffmpeg",
    }
    return "\n".join(f"{k}={shlex.quote(str(v))}" for k, v in pairs.items())


def main(argv):
    import argparse
    parser = argparse.ArgumentParser(prog="stick_config.py", description="Host configuration (docs/configuration.md)")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show"); show.add_argument("--json", action="store_true")
    sub.add_parser("validate")
    shell = sub.add_parser("shell"); shell.add_argument("--no-server", action="store_true", help="do not require advertise_host (tools that only need locations)")
    dev = sub.add_parser("device-config"); dev.add_argument("--out")
    init = sub.add_parser("init"); init.add_argument("--advertise-host", required=True); init.add_argument("--keep-existing-locations", action="store_true")
    init.add_argument("--project", help="write config/host.conf into THIS project folder instead of the one this script lives in (to prepare an installation before upgrading it)")
    sub.add_parser("suggest-address")
    args = parser.parse_args(argv)
    try:
        if args.command == "suggest-address":
            found = local_ipv4_addresses()
            print("\n".join(found) if found else "no network address found")
            return 0
        if args.command == "init":
            target = init_file(args.project or PROJECT_ROOT, args.advertise_host, args.keep_existing_locations)
            print(f"Wrote {target}. Check it, then run: python3 tools/preflight.py")
            return 0
        cfg = load(require_server=not (args.command == "shell" and args.no_server))
        if args.command == "validate":
            print("Configuration is valid.")
        elif args.command == "show":
            data = cfg.as_dict()
            data["sources"] = cfg.sources
            if args.json:
                print(json.dumps(data, indent=2, sort_keys=True))
            else:
                for key in KEYS:
                    print(f"{key:15s} {data[key] if data[key] is not None else '-':50s} ({cfg.sources.get(key)})")
                print(f"{'config_file':15s} {data['config_file'] or '- (no file; environment and defaults only)'}")
        elif args.command == "shell":
            print(shell_lines(cfg))
        elif args.command == "device-config":
            text = json.dumps(device_config(cfg), indent=2, sort_keys=True) + "\n"
            if args.out:
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text, encoding="utf-8")
                print(f"Wrote {out} (server address and port only; nothing else goes to the device)")
            else:
                sys.stdout.write(text)
        return 0
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
