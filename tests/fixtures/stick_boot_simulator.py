"""The simulated Stick's NETWORK side after a restart, for tests/test_setup_wizard.py (with the REAL, secured audio_server.py).

    python stick_boot_simulator.py --device-port N --device-state FILE --flash DIR --log FILE

tests/fixtures/fake_stick_device.py counts restarts in its state file. Each time that count grows, this process plays what the real Stick
does after a boot, over HTTP, if the Stick could really start the app (boot_option 0, main.py, Wi-Fi saved and joinable, server
reachable): an authenticated POST /device/sync with the key file on its simulated /flash, then an authenticated audio upload of silence
followed by its status, and, as a check, the same upload without the key. Each outcome is one JSON line in --log. It never prints the key.
"""
import argparse
import http.client
import json
import time
from pathlib import Path


def request(port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    answer = conn.getresponse()
    data = answer.read()
    conn.close()
    return answer.status, data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-port", type=int, required=True)
    for name in ("--device-state", "--flash", "--log"):
        parser.add_argument(name, required=True)
    args = parser.parse_args()
    state_path, flash = Path(args.device_state), Path(args.flash)
    seen = json.loads(state_path.read_text()).get("resets", 0)
    boots = 0
    while True:
        time.sleep(0.3)
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            continue
        if state.get("resets", 0) <= seen:
            continue
        seen = state.get("resets", 0)
        nvs, key_file = state.get("nvs", {}), flash / "device_key.txt"
        if not (nvs.get("boot_option") == 0 and (flash / "main.py").exists() and nvs.get("ssid0") and nvs.get("pswd0")
                and state.get("wifi_result", "ok") == "ok" and state.get("server_reachable") and key_file.exists()):
            continue
        boots += 1
        key = key_file.read_text().strip()
        result = {"boot": boots}
        try:
            body = json.dumps({"proto": 1, "store": "", "rev": 0, "notif": 0, "boot": "s%07d" % boots, "uptime": 3}).encode()
            result["sync"] = request(args.device_port, "POST", "/device/sync", body, {"X-Device-Key": key, "Content-Type": "application/json"})[0]
            silence = bytes(32000)
            result["audio_without_key"] = request(args.device_port, "POST", "/audio", silence, {"Content-Type": "application/octet-stream"})[0]
            status, data = request(args.device_port, "POST", "/audio", silence, {"X-Device-Key": key, "Content-Type": "application/octet-stream"})
            result["audio"] = status
            if status == 200:
                job = json.loads(data)["id"]
                for _ in range(100):
                    status, data = request(args.device_port, "GET", f"/status?id={job}", headers={"X-Device-Key": key})
                    if status != 200 or json.loads(data).get("state") != "pending":
                        break
                    time.sleep(0.2)
                result["status"] = status
                result["state"] = json.loads(data).get("state") if status == 200 else None
        except (OSError, ValueError, http.client.HTTPException) as error:
            result["error"] = type(error).__name__
        with open(args.log, "a") as log:
            log.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
