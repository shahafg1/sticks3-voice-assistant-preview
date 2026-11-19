"""An ISOLATED stand-in for the assistant's server, for tests/test_setup_wizard.py.

    python fake_assistant_server.py --panel-port N --device-port N --data DIR --secrets DIR --device-state FILE --flash DIR  <project>/audio_server.py

The last argument only names the project whose server this stands in for, so that its command line ends with that project's
audio_server.py exactly as the real launchers start it (the wizard and the launchers recognise "this folder's server" that way).
It runs the REAL device_link.DeviceLink on temporary folders: the key is read once at start-up, as the real server does. Its status
answers on 127.0.0.1:<panel port>; the device port only accepts and closes connections. When the simulated Stick (FAKE_DEVICE_STATE)
has been restarted, it receives that Stick's report, authenticated with the key file on its simulated /flash, if the Stick could
really start the app (boot_option 0, main.py, Wi-Fi saved and joinable, server reachable).
"""
import argparse
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
import device_link as dl  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    for name in ("--panel-port", "--device-port"):
        parser.add_argument(name, type=int, required=True)
    for name in ("--data", "--secrets", "--device-state", "--flash"):
        parser.add_argument(name, required=True)
    parser.add_argument("label")
    args = parser.parse_args()
    link = dl.DeviceLink(lambda after, ip: None, data_dir=Path(args.data), secrets_dir=Path(args.secrets))
    flash = Path(args.flash)
    seen = [json.loads(Path(args.device_state).read_text()).get("resets", 0)]
    boots = [0]
    lock = threading.Lock()

    def tick():
        with lock:
            state = json.loads(Path(args.device_state).read_text())
            if state.get("resets", 0) <= seen[0]:
                return
            seen[0] = state.get("resets", 0)
            nvs = state.get("nvs", {})
            key = flash / "device_key.txt"
            if (nvs.get("boot_option") == 0 and (flash / "main.py").exists() and nvs.get("ssid0") and nvs.get("pswd0")
                    and state.get("wifi_result", "ok") == "ok" and state.get("server_reachable") and key.exists()):
                boots[0] += 1
                body = {"proto": 1, "store": "", "rev": 0, "notif": 0, "boot": "w%07d" % boots[0], "uptime": 3}
                link.sync(key.read_text().strip(), json.dumps(body).encode(), "192.0.2.30")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/status":
                self.send_response(404)
                self.end_headers()
                return
            tick()
            body = json.dumps(link.snapshot(), default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    device = socket.socket()
    device.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    device.bind(("127.0.0.1", args.device_port))
    device.listen(4)

    def accept():
        while True:
            conn, _ = device.accept()
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", args.panel_port), Handler).serve_forever()


if __name__ == "__main__":
    main()
