"""A SIMULATED StickS3 for tests/test_stick_backup.py, answering the Stick-tool commands tools/stick_backup.py sends:

    <tool> connect PORT resume exec CODE
    <tool> connect PORT resume exec --no-follow CODE         (a restart request: only counted)
    <tool> connect PORT resume fs --no-verbose cp :/flash/PATH LOCAL
    <tool> connect PORT resume fs --no-verbose cp LOCAL :/flash/PATH         (only when the state says "writable": true)

The device is described by a JSON state file (FAKE_DEVICE_STATE): a folder standing in for /flash, os.uname() fields, UIFlow's
firmware_info(), NVS values, the chip's unique id, and faults to inject. `exec` RUNS the tool's snippet in CPython with stand-ins for
os/esp32/machine/open that map /flash to that folder (read-only), so the snippets' logic is exercised; this says nothing about
MicroPython itself or a real Stick. Every call is appended to FAKE_DEVICE_LOG (arguments only), and every write as ["WRITE", operation, path...]. Unless the state has
"writable": true, the simulated /flash is read-only: writes fail with EROFS and are logged as WRITE-REFUSED. Its rename never
replaces an existing file (as on FAT), so the tool must not rely on that.

For tools/stick_setup.py it also simulates: NVS writes (set_u8 + commit, with "nvs_set_fail" / "nvs_commit_lost" faults; logged as
NVS-SET without string values), a Wi-Fi radio that joins with the saved credentials ("wifi_result": ok | wrong_password | no_ap |
timeout), a socket whose connect succeeds when "server_reachable" is true, and a virtual clock (sleeps cost no real time). A key-like
file (device_key*) is logged as KEY-WRITTEN-ON-DEVICE / KEY-OPENED-ON-DEVICE whenever it is written or read; its bytes are never logged.
"""
import builtins
import json
import os
import shutil
import sys
import time
import types

NOT_FOUND = -4354          # what an ESP-IDF "NVS key not found" looks like through MicroPython's OSError (an assumption of this stand-in)


def load():
    path = os.environ["FAKE_DEVICE_STATE"]
    with open(path) as handle:
        state = json.load(handle)
    state["calls"] = state.get("calls", 0) + 1
    return path, state


def save(path, state):
    with open(path, "w") as handle:
        json.dump(state, handle)


def fail(message):
    print(message, file=sys.stderr)
    return 1


def flash_path(state, path):
    if not (path == "/flash" or path.startswith("/flash/")):
        raise OSError(2, "ENOENT")
    real = os.path.normpath(os.path.join(state["flash_dir"], path[len("/flash"):].lstrip("/")))
    if not (real == os.path.normpath(state["flash_dir"]) or real.startswith(os.path.normpath(state["flash_dir"]) + os.sep)):
        raise OSError(2, "ENOENT")
    return real


def log(entry):
    with open(os.environ["FAKE_DEVICE_LOG"], "a") as handle:
        handle.write(json.dumps(entry) + "\n")


class Interrupted(Exception):
    """A simulated loss of the connection part-way through a device-side snippet."""


def key_like(path):
    return isinstance(path, str) and path.rsplit("/", 1)[-1].lower().startswith("device_key")


def writes_allowed(state, operation, *paths):
    if any(key_like(p) for p in paths):
        log(["KEY-WRITTEN-ON-DEVICE", operation])
    if not state.get("writable"):
        log(["WRITE-REFUSED", operation, *paths])
        raise OSError(30, "EROFS")
    log(["WRITE", operation, *paths])


def run_snippet(state, code):
    faults = state.get("faults", {})
    uname = dict(state.get("uname") or {})
    renames = [0]

    def ilistdir(path):
        real = flash_path(state, path)
        for name in sorted(os.listdir(real)):
            full = os.path.join(real, name)
            if os.path.isdir(full):
                yield (name, 0x4000, 0, 0)
            else:
                yield (name, 0x8000, 0, os.path.getsize(full) + faults.get("inflate_sizes", 0))

    def stat(path):
        real = flash_path(state, path)
        if not os.path.exists(real):
            raise OSError(2, "ENOENT")
        return os.stat(real)

    def os_uname():
        if faults.get("uname_error"):
            raise OSError(5, "EIO")
        return types.SimpleNamespace(**uname)

    def mkdir(path):
        writes_allowed(state, "mkdir", path)
        os.mkdir(flash_path(state, path))

    def rename(old, new):
        writes_allowed(state, "rename", old, new)
        if os.path.lexists(flash_path(state, new)):
            raise OSError(17, "EEXIST")
        os.rename(flash_path(state, old), flash_path(state, new))
        if new in faults.get("corrupt_after_activation", []):
            with open(flash_path(state, new), "ab") as handle:
                handle.write(b"!")
        renames[0] += 1
        if faults.get("activation_interrupt_after_renames") == renames[0]:
            raise Interrupted("connection lost")

    def remove(path):
        writes_allowed(state, "remove", path)
        os.remove(flash_path(state, path))

    def rmdir(path):
        writes_allowed(state, "rmdir", path)
        os.rmdir(flash_path(state, path))

    def statvfs(path):
        flash_path(state, path)
        free = int(state.get("flash_free", 4 * 1024 * 1024)) // 4096
        return (4096, 4096, 1024, free, free, 0, 0, 0, 0, 255)

    fake_os = types.SimpleNamespace(uname=os_uname, ilistdir=lambda p: list(ilistdir(p)), stat=stat, mkdir=mkdir, rename=rename,
                                    remove=remove, rmdir=rmdir, statvfs=statvfs)

    class NVS:
        def __init__(self, namespace):
            self.values = state.get("nvs", {}) if namespace == "uiflow" else {}

        def _get(self, key):
            errors = faults.get("nvs_errors", {})
            if errors.get(key) == "error":
                raise OSError(5, "EIO")
            if key not in self.values:
                raise OSError(NOT_FOUND)
            return self.values[key]

        def get_u8(self, key):
            return int(self._get(key))

        def get_str(self, key):
            return str(self._get(key))

        def set_u8(self, key, value):
            log(["NVS-SET", key, value])
            if not state.get("writable"):
                raise OSError(30, "EROFS")
            if faults.get("nvs_set_fail"):
                raise OSError(5, "EIO")
            self.pending = dict(getattr(self, "pending", {}), **{key: int(value)})

        def set_str(self, key, value):
            log(["NVS-SET-STR", key])                                    # never the value
            raise OSError(1, "the simulated Stick refuses string writes: no tool may write Wi-Fi settings")

        def commit(self):
            if faults.get("nvs_commit_lost"):
                return
            state.setdefault("nvs", {}).update(getattr(self, "pending", {}))
            save(os.environ["FAKE_DEVICE_STATE"], state)

    def firmware_info():
        if faults.get("firmware_info_error"):
            raise AttributeError("firmware_info")
        return tuple(state.get("firmware_info") or ())

    unique = state["unique_id"]
    if faults.get("swap_id_after_calls") is not None and state["calls"] > faults["swap_id_after_calls"]:
        unique = state["other_unique_id"]
    fake_esp32 = types.SimpleNamespace(NVS=NVS, firmware_info=firmware_info)
    fake_machine = types.SimpleNamespace(unique_id=lambda: bytes.fromhex(unique))
    clock = [0]

    def sleep_ms(ms):
        clock[0] += int(ms)

    fake_time = types.SimpleNamespace(ticks_ms=lambda: clock[0], ticks_diff=lambda a, b: a - b, sleep_ms=sleep_ms,
                                      sleep=lambda s: sleep_ms(s * 1000), time=time.time)

    class WLAN:
        def __init__(self, interface):
            self.joined = bool(state.get("wifi_connected"))
            self.code = 1010 if self.joined else 1000

        def active(self, *a):
            if a and a[0]:
                log(["WIFI-ACTIVATE"])
            return True

        def isconnected(self):
            return self.joined

        def connect(self, ssid, password):
            log(["WIFI-JOIN"])
            nvs = state.get("nvs", {})
            if ssid != nvs.get("ssid0") or password != nvs.get("pswd0"):
                raise AssertionError("the check must join with the SAVED credentials")
            result = state.get("wifi_result", "ok")
            self.joined = result == "ok"
            self.code = {"ok": 1010, "wrong_password": 202, "no_ap": 201}.get(result, 1001)

        def status(self, *a):
            return self.code

        def ifconfig(self):
            return ("192.0.2.30", "255.255.255.0", "192.0.2.1", "192.0.2.1") if self.joined else ("0.0.0.0",) * 4

    fake_network = types.SimpleNamespace(WLAN=WLAN, STA_IF=0, STAT_WRONG_PASSWORD=202, STAT_NO_AP_FOUND=201, STAT_GOT_IP=1010)

    class Socket:
        def settimeout(self, s):
            pass

        def connect(self, address):
            log(["SOCKET-CONNECT", list(address)])
            if not state.get("server_reachable"):
                raise OSError(113, "EHOSTUNREACH")

        def close(self):
            pass

    fake_socket = types.SimpleNamespace(socket=Socket, getaddrinfo=lambda host, port: [(2, 1, 0, "", (host, port))])
    fakes = {"os": fake_os, "uos": fake_os, "esp32": fake_esp32, "machine": fake_machine, "time": fake_time, "network": fake_network,
             "socket": fake_socket}
    real_import = builtins.__import__

    def importer(name, globals=None, locals=None, fromlist=(), level=0):
        if name in fakes:
            return fakes[name]
        if name in ("json", "hashlib", "binascii", "sys"):
            return real_import(name, globals, locals, fromlist, level)
        raise ImportError(name)

    def fake_open(path, mode="r", *a, **k):
        if any(c in mode for c in "wax+"):
            raise OSError(30, "EROFS (the simulated Stick is read-only for this tool)")
        if key_like(path):
            with open(os.environ["FAKE_DEVICE_LOG"], "a") as log:
                log.write(json.dumps(["KEY-OPENED-ON-DEVICE"]) + "\n")
        return open(flash_path(state, path), mode, *a, **k)

    lines = []
    printer = lambda *a, **k: lines.append(" ".join(str(x) for x in a))   # noqa: E731
    env = {"__builtins__": dict(vars(builtins), __import__=importer, open=fake_open, print=printer), "__name__": "__main__"}
    marker = faults.get("hang_on_exec_marker")
    if marker and marker in code:
        time.sleep(60)
    try:
        exec(code, env)
    except Interrupted:
        print("\n".join(lines))
        return fail("mpremote: device disconnected")
    except Exception as error:                                           # a traceback on the device: mpremote exits non-zero
        print("\n".join(lines))
        return fail("Traceback (most recent call last):\n" + type(error).__name__ + ": " + str(error))
    sent = "\n".join(lines)                                              # what would leave the Stick over USB
    nvs = state.get("nvs", {})
    if any(v and str(v) in sent for v in (nvs.get("ssid0"), nvs.get("pswd0"))):
        log(["WIFI-VALUE-LEFT-THE-STICK"])
    for name in os.listdir(state["flash_dir"]):
        if key_like(name) and os.path.isfile(os.path.join(state["flash_dir"], name)):
            text = open(os.path.join(state["flash_dir"], name), "rb").read().strip().decode(errors="replace")
            if text and text in sent:
                log(["KEY-LEFT-THE-STICK"])
    cut = faults.get("truncate_exec_output_after_lines")
    if cut is not None and any(line.startswith("STICKLS") for line in lines):
        print("\n".join(lines[:cut]))
        return fail("mpremote: device disconnected")
    print("\n".join(lines))
    return 0


def main(argv):
    path, state = load()
    with open(os.environ["FAKE_DEVICE_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\n")
    save(path, state)
    if len(argv) < 4 or argv[0] != "connect" or argv[2] != "resume":
        return fail("usage: connect PORT resume ...")
    port, command = argv[1], argv[3:]
    faults = state.get("faults", {})
    if not os.path.exists(port):
        return fail(f"mpremote: failed to access {port}")
    if faults.get("disconnect_after_calls") is not None and state["calls"] > faults["disconnect_after_calls"]:
        os.remove(port)                                                    # the device node disappears, as when the cable is pulled
        return fail(f"mpremote: failed to access {port}")
    if faults.get("not_micropython"):
        return fail("could not enter raw repl")
    if command[0] == "exec":
        if command[1] == "--no-follow":
            state["resets"] = state.get("resets", 0) + 1
            save(path, state)
            return 0
        return run_snippet(state, command[1])
    if command[:3] == ["fs", "--no-verbose", "cp"] and command[4].startswith(":"):
        source, device_path = command[3], command[4][1:]
        if faults.get("hang_on_cp"):
            time.sleep(faults["hang_on_cp"])
        if any(device_path.endswith("/stage/" + p[len("/flash/"):]) for p in faults.get("cp_to_fail", [])) or device_path in faults.get("cp_fail_to", []):
            return fail("mpremote: OSError: [Errno 5] EIO")
        try:
            writes_allowed(state, "cp", device_path)
            real = flash_path(state, device_path)
        except OSError as error:
            return fail(f"mpremote: OSError: {error}")
        with open(source, "rb") as src, open(real, "wb") as dst:
            data = src.read()
            if any(device_path.endswith("/stage/" + p[len("/flash/"):]) for p in faults.get("cp_to_truncate", [])) or device_path in faults.get("cp_truncate_to", []):
                data = data[: len(data) // 2]
            dst.write(data)
        return 0
    if command[:3] == ["fs", "--no-verbose", "cp"]:
        source, target = command[3], command[4]
        device_path = source[1:]
        with open(os.environ["FAKE_DEVICE_LOG"], "a") as log:
            log.write(json.dumps(["COPY", device_path]) + "\n")
        if faults.get("hang_on_cp"):
            time.sleep(faults["hang_on_cp"])
        if device_path in faults.get("cp_fail", []):
            return fail("mpremote: OSError: [Errno 5] EIO")
        real = flash_path(state, device_path)
        if device_path in faults.get("truncate_files", []):
            with open(real, "rb") as src, open(target, "wb") as dst:
                data = src.read()
                dst.write(data[: len(data) // 2])
            return 0
        shutil.copyfile(real, target)
        return 0
    return fail("unsupported command in the simulated Stick: " + " ".join(command[:3]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
