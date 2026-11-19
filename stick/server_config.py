# server_config.py - where this Stick finds the Mac server (pure logic: no hardware, no network).
#
# /flash/device_config.json is generated on the Mac by `python3 stick_config.py device-config` and holds ONLY the server address and port:
#   {"v": 1, "server_host": "<the Mac's LAN address>", "server_port": 8000}
# No host paths, no Wi-Fi credentials and no authentication secret belong in it (the device key has its own file, device_key.txt).
# There is no built-in address: without a valid file the app shows "No server / Setup needed" and sends nothing anywhere.
import json

CONFIG_FILE = "/flash/device_config.json"
VERSION = 1
HOST_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-"


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def parse(text):
    """(host, port) from the file's text, or ValueError with the reason (short, ASCII)."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("not valid JSON")
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    if data.get("v") != VERSION or isinstance(data.get("v"), bool):
        raise ValueError("unsupported version")
    host, port = data.get("server_host"), data.get("server_port")
    if not isinstance(host, str) or not host or len(host) > 253 or any(c not in HOST_CHARS for c in host):
        raise ValueError("bad server_host")
    if host in ("0.0.0.0", "localhost") or host.startswith("127."):
        raise ValueError("server_host is not reachable from the Stick")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("bad server_port")
    return host, port


def load(read=read_text):
    """(host, port, None) when usable, else (None, None, reason)."""
    text = read(CONFIG_FILE)
    if text is None:
        return None, None, "missing " + CONFIG_FILE
    try:
        host, port = parse(text)
    except ValueError as error:
        return None, None, str(error)
    return host, port, None
