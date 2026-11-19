"""Tests for tools/stick_setup.py (Wi-Fi checks, device-key provisioning, auto-start, pairing) against a SIMULATED Stick and an
ISOLATED server. Run: python3 tests/test_stick_setup.py

Synthetic everything: the Stick is tests/fixtures/fake_stick_device.py (its Wi-Fi radio, socket and NVS writes are simulated), the
Wi-Fi values and keys are sentinels or random per test, and the "server" is the real device_link.DeviceLink on temporary folders,
answering /api/status on a free local port. When the tool restarts the simulated Stick, the server receives that Stick's report
(authenticated with the key file on its simulated /flash) only if the Stick could really start the app: boot_option 0, main.py
present, Wi-Fi saved and joinable, and the server reachable. The isolation guard is active in every process."""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import test_stick_backup as tb
import test_stick_install as ti

ROOT = tb.ROOT
sys.path.insert(0, str(ROOT))
import device_link as dl  # noqa: E402

TOOL = ROOT / "tools" / "stick_setup.py"
INSTALL = ROOT / "tools" / "stick_install.py"
KEY_FILE, KEY_PART = "device_key.txt", "device_key.txt.part"
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class Server:
    """An isolated server: the real DeviceLink on temporary folders, its status on a free local port. restart() reloads the key."""

    def __init__(self, lab, secrets_dir):
        self.lab, self.secrets = lab, secrets_dir
        self.data = Path(lab.tmp.name) / "server-data"
        self.link = self.make()
        self.seen = lab.state().get("resets", 0)
        self.boots = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.tick()
                body = json.dumps(outer.link.snapshot(), default=str).encode()
                self.send_response(200 if self.path == "/api/status" else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def make(self):
        return dl.DeviceLink(lambda after, ip: None, data_dir=self.data, secrets_dir=self.secrets)

    def restart(self):
        self.link = self.make()

    def tick(self):
        """After each restart the tool sends, the simulated Stick reports, if it really could."""
        state = self.lab.state()
        resets = state.get("resets", 0)
        if resets <= self.seen:
            return
        self.seen = resets
        nvs = state.get("nvs", {})
        key = self.lab.flash / KEY_FILE
        if (nvs.get("boot_option") == 0 and (self.lab.flash / "main.py").exists() and nvs.get("ssid0") and nvs.get("pswd0")
                and state.get("wifi_result", "ok") == "ok" and state.get("server_reachable") and key.exists()):
            self.boots += 1
            body = {"proto": 1, "store": "", "rev": 0, "notif": 0, "boot": "b%07d" % self.boots, "uptime": 3}
            self.link.sync(key.read_text().strip(), json.dumps(body).encode(), "192.0.2.30")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Lab(ti.Lab):
    """A simulated Stick (see test_stick_install.Lab) plus this installation's private secrets folder and, optionally, a server."""

    def __init__(self, scenario="current", key=None, server=True, **state):
        state.setdefault("server_reachable", True)
        super().__init__(scenario, **state)
        (self.flash / KEY_FILE).unlink(missing_ok=True)
        if key is not None:
            (self.flash / KEY_FILE).write_text(key + "\n")
        self.secrets = Path(self.tmp.name) / "secrets"
        self.server = None
        self.panel = free_port()
        if server:
            self.start_server()

    def start_server(self):
        self.server = Server(self, self.secrets)
        self.panel = self.server.port

    def env(self, **more):
        return super().env(**{"STICK_SECRETS_DIR": str(self.secrets), "STICK_PANEL_PORT": str(self.panel), **more})

    def setup(self, *args, timeout=180, **env):
        return self.run(*args, tool=TOOL, timeout=timeout, **env)

    def mac_key(self, text):
        self.secrets.mkdir(mode=0o700, exist_ok=True)
        (self.secrets / "device_key").write_text(text + "\n")
        os.chmod(self.secrets / "device_key", 0o600)

    def mac_key_text(self):
        path = self.secrets / "device_key"
        return path.read_text().strip() if path.exists() else None

    def nvs(self):
        return self.state().get("nvs", {})

    def nvs_writes(self):
        return [c for c in self.calls() if c and c[0] in ("NVS-SET", "NVS-SET-STR")]

    def setup_records(self, kind=None):
        root = self.data / "stick-setup"
        found = [json.loads(p.read_text()) for p in sorted(root.glob("stick-*/*/record.json"), key=lambda p: os.stat(p.parent).st_birthtime)] \
            if root.exists() else []
        return [r for r in found if kind is None or r.get("kind") == kind]

    def close(self):
        if self.server:
            self.server.close()
        super().close()


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lab_case(fn=None, **kw):
    def wrap(f):
        def run():
            lab = Lab(**kw)
            try:
                f(lab)
            finally:
                lab.close()
        run.__name__ = f.__name__
        return case(run)
    return wrap(fn) if fn else wrap


def device():
    return hashlib.sha256(bytes.fromhex("a1b2c3d4e5f6")).hexdigest()[:12]


def random_key():
    return secrets.token_hex(32)


def no_key_leak(lab, *texts):
    """Neither key (the Mac's or the Stick's), nor any Wi-Fi value, in any output, Stick-tool call, or record."""
    keys = [k for k in (lab.mac_key_text(), (lab.flash / KEY_FILE).read_text().strip() if (lab.flash / KEY_FILE).exists() else None) if k]
    records = "".join(p.read_text() for p in lab.data.rglob("*.json")) if lab.data.exists() else ""
    everything = list(texts) + [lab.log.read_text(), records]
    assert not any(c and c[0] in ("WIFI-VALUE-LEFT-THE-STICK", "KEY-LEFT-THE-STICK") for c in lab.calls()), "a value left the Stick"
    tb.no_secrets(*everything)
    for key in keys:
        for text in everything:
            assert key not in text, "a device key leaked"
            assert hashlib.sha256(key.encode()).hexdigest() not in text, "a reusable key hash leaked"


def out_of(done):
    return done.stdout + done.stderr


# ---- the plan and read-only checks --------------------------------------------------------------------------------------------
@lab_case
def the_plan_is_offline_and_gives_the_order_and_the_button_a_warning(lab):
    done = lab.setup("plan")
    out = out_of(done)
    assert done.returncode == 0 and lab.calls() == [], out
    order = [out.index(word) for word in ("Wi-Fi:", "Backup:", "Files:", "Key:", "Auto-start:", "Verify:")]
    assert order == sorted(order), out
    assert "REMOVES /flash/main.py" in out and "autostart --off" in out and "never shown" in out


@lab_case(key=None)
def status_keeps_saved_connected_key_and_pairing_apart_and_writes_nothing(lab):
    done = lab.setup("status", "--port", lab.port)
    out = out_of(done)
    assert done.returncode == 0, out
    assert "saved is not connected" in out and "Pairing:            NOT confirmed" in out and "Device key:         on the Stick absent" in out
    assert "NEXT STEP: provision the device key" in out
    assert lab.writes() == [] and lab.nvs_writes() == []
    no_key_leak(lab, out)


@lab_case(key=None)
def status_never_counts_a_present_key_as_pairing(lab):
    k = random_key()
    lab.mac_key(k)
    (lab.flash / KEY_FILE).write_text(k + "\n")
    lab.server.restart()
    out = out_of(lab.setup("status", "--port", lab.port))
    assert "same key yes" in out and "NOT confirmed" in out and "verify" in out, out
    no_key_leak(lab, out)


# ---- Wi-Fi ---------------------------------------------------------------------------------------------------------------------
@lab_case
def wifi_check_reports_saved_connected_and_reachable_separately(lab):
    done = lab.setup("wifi", "--port", lab.port)
    out = out_of(done)
    assert done.returncode == 0, out
    assert "Credentials saved:      name yes, password yes" in out and "Network connected:      yes" in out
    assert "Server port reachable:  yes at 192.0.2.10:50001" in out and "Authenticated contact:  not checked here" in out
    assert lab.writes() == [] and lab.nvs_writes() == []
    no_key_leak(lab, out)


@lab_case(wifi_result="wrong_password")
def wifi_check_names_a_failed_join_and_tries_no_further(lab):
    done = lab.setup("wifi", "--port", lab.port)
    out = out_of(done)
    assert done.returncode == 1 and "Network connected:      NO" in out and "wrong password" in out and "reachable:  not tried" in out, out
    assert not any(c and c[0] == "SOCKET-CONNECT" for c in lab.calls())
    no_key_leak(lab, out)


@lab_case(server_reachable=False)
def wifi_check_separates_a_joined_network_from_an_unreachable_server(lab):
    out = out_of(lab.setup("wifi", "--port", lab.port))
    assert "Network connected:      yes" in out and "Server port reachable:  no" in out, out


@lab_case(wifi_result="timeout")
def wifi_check_is_bounded(lab):
    done = lab.setup("wifi", "--port", lab.port, timeout=60)
    out = out_of(done)
    assert done.returncode == 1 and "within 20 s" in out and "connecting" in out, out


@lab_case(scenario="fresh")
def wifi_check_with_nothing_saved_points_to_uiflow_setup(lab):
    lab.set(nvs={"boot_option": 1})
    done = lab.setup("wifi", "--port", lab.port)
    out = out_of(done)
    assert done.returncode == 1 and "name no, password no" in out and "UIFlow's Setup app" in out and "not tried" in out, out
    assert not any(c and c[0] == "SOCKET-CONNECT" for c in lab.calls())


@lab_case(wifi_connected=True)
def reuse_only_uses_the_existing_connection_and_never_switches_wifi_on_or_joins(lab):
    done = lab.setup("wifi", "--port", lab.port, "--reuse-only")
    out = out_of(done)
    assert done.returncode == 0 and "it already was; reused" in out and "Server port reachable:  yes" in out, out
    assert not any(c and c[0] in ("WIFI-ACTIVATE", "WIFI-JOIN") for c in lab.calls()) and lab.writes() == [] and lab.nvs_writes() == []
    no_key_leak(lab, out)


@lab_case
def reuse_only_without_a_connection_reports_it_and_joins_nothing(lab):
    done = lab.setup("wifi", "--port", lab.port, "--reuse-only")
    out = out_of(done)
    assert done.returncode == 1 and "not joined: --reuse-only" in out and "not tried" in out, out
    assert not any(c and c[0] in ("WIFI-ACTIVATE", "WIFI-JOIN", "SOCKET-CONNECT") for c in lab.calls())
    lab.log.write_text("")
    lab.setup("wifi", "--port", lab.port)                                     # the default may switch Wi-Fi on and join
    assert any(c and c[0] == "WIFI-JOIN" for c in lab.calls())


# ---- the device key -----------------------------------------------------------------------------------------------------------
@lab_case(key=None)
def key_needs_a_backup_and_then_installs_the_same_key_on_both_sides(lab):
    refused = lab.setup("key", "--port", lab.port, "--expect-device", device())
    assert refused.returncode == 1 and "backup" in refused.stderr and lab.mac_key_text() is None and lab.writes() == [], out_of(refused)
    folder = lab.backup()
    done = lab.setup("key", "--port", lab.port, "--expect-device", device(), "--backup", str(folder))
    out = out_of(done)
    assert done.returncode == 0 and "DEVICE KEY INSTALLED" in out and "Pairing is NOT confirmed" in out and "start the server so it loads" in out, out
    mac = lab.mac_key_text()
    assert mac and len(mac) == 64 and (lab.flash / KEY_FILE).read_text().strip() == mac and not (lab.flash / KEY_PART).exists()
    assert oct((lab.secrets / "device_key").stat().st_mode & 0o777) == "0o600" and oct(lab.secrets.stat().st_mode & 0o777) == "0o700"
    for call in lab.calls():                                                   # the key travels only as a file path, never as text
        assert mac not in json.dumps(call)
    [rec] = lab.setup_records("device-key")
    assert rec["status"] == "complete" and rec["mac_key"] == "created" and rec["never"][0].startswith("Wi-Fi credentials")
    no_key_leak(lab, out)


@lab_case(key=None)
def a_repeated_key_run_writes_nothing(lab):
    k = random_key()
    lab.mac_key(k)
    (lab.flash / KEY_FILE).write_text(k + "\n")
    done = lab.setup("key", "--port", lab.port, "--expect-device", device())
    assert done.returncode == 0 and "Nothing to do" in done.stdout and "NOT pairing" in done.stdout, out_of(done)
    assert lab.writes() == [] and lab.mac_key_text() == k


@lab_case(key=None)
def a_different_key_on_the_stick_stops_and_is_preserved(lab):
    theirs, ours = random_key(), random_key()
    (lab.flash / KEY_FILE).write_text(theirs + "\n")
    lab.mac_key(ours)
    done = lab.setup("key", "--port", lab.port, "--expect-device", device(), "--backup", str(lab.backup()))
    out = out_of(done)
    assert done.returncode == 1 and "DIFFERENT" in out and "rotation" in out and "not implemented" in out, out
    assert (lab.flash / KEY_FILE).read_text().strip() == theirs and lab.mac_key_text() == ours and lab.writes() == []
    no_key_leak(lab, out)


@lab_case(key=None)
def a_stick_key_with_no_mac_key_stops_without_creating_one(lab):
    theirs = random_key()
    (lab.flash / KEY_FILE).write_text(theirs + "\n")
    done = lab.setup("key", "--port", lab.port, "--expect-device", device(), "--backup", str(lab.backup()))
    out = out_of(done)
    assert done.returncode == 1 and "this installation has none" in out and "no key was created" in out, out
    assert lab.mac_key_text() is None and (lab.flash / KEY_FILE).read_text().strip() == theirs and lab.writes() == []


@lab_case(key=None)
def an_interrupted_copy_is_finished_by_a_rerun_with_the_same_key(lab):
    folder = lab.backup()
    lab.set(faults={"cp_fail_to": ["/flash/" + KEY_PART]})
    first = lab.setup("key", "--port", lab.port, "--expect-device", device(), "--backup", str(folder))
    assert first.returncode == 1 and "run `key` again" in out_of(first) and not (lab.flash / KEY_FILE).exists(), out_of(first)
    mac = lab.mac_key_text()
    assert mac
    lab.set(faults={"cp_truncate_to": ["/flash/" + KEY_PART]})
    second = lab.setup("key", "--port", lab.port, "--expect-device", device(), "--backup", str(folder))
    assert second.returncode == 1 and "did not verify" in out_of(second) and not (lab.flash / KEY_FILE).exists(), out_of(second)
    assert (lab.flash / KEY_PART).exists() and lab.mac_key_text() == mac
    status = out_of(lab.setup("status", "--port", lab.port))
    assert "Partial key copy" in status, status
    lab.set(faults={})
    third = lab.setup("key", "--port", lab.port, "--expect-device", device(), "--backup", str(folder))
    assert third.returncode == 0, out_of(third)
    assert lab.mac_key_text() == mac and (lab.flash / KEY_FILE).read_text().strip() == mac and not (lab.flash / KEY_PART).exists()
    assert [r["status"] for r in lab.setup_records("device-key")] == ["failed", "failed", "complete"]
    no_key_leak(lab, out_of(first), out_of(second), out_of(third), status)


@lab_case(key=None)
def key_refuses_a_shared_secrets_folder_and_the_wrong_device(lab):
    lab.secrets.mkdir(mode=0o755)
    os.chmod(lab.secrets, 0o755)
    done = lab.setup("key", "--port", lab.port, "--expect-device", device())
    assert done.returncode == 2 and "readable by other users" in done.stderr and lab.calls() == [], out_of(done)
    os.chmod(lab.secrets, 0o700)
    done = lab.setup("key", "--port", lab.port, "--expect-device", "000000000000")
    assert done.returncode == 1 and "not 000000000000" in done.stderr and lab.writes() == [] and lab.mac_key_text() is None


@lab_case
def backups_never_copy_a_partial_key(lab):
    (lab.flash / KEY_PART).write_text(random_key() + "\n")
    folder = lab.backup()
    manifest = lab.manifest(folder)
    assert "/flash/" + KEY_PART in [e["path"] for e in manifest["excluded"]]
    assert not any(KEY_PART in str(p) for p in folder.rglob("*"))


@lab_case(key=None)
def key_check_compares_and_never_writes_even_with_a_backup(lab):
    folder = lab.backup()
    lab.log.write_text("")
    for setup, expect in (("no keys", "this installation has no key"), ("mac only", "the Stick has no key file"),
                          ("different", "the keys are DIFFERENT"), ("same", None)):
        if setup == "mac only":
            lab.mac_key(random_key())
        if setup == "different":
            (lab.flash / KEY_FILE).write_text(random_key() + "\n")
        if setup == "same":
            (lab.flash / KEY_FILE).write_text(lab.mac_key_text() + "\n")
        mac_before = lab.mac_key_text()
        done = lab.setup("key", "--port", lab.port, "--expect-device", device(), "--check", "--backup", str(folder))
        out = out_of(done)
        if expect:
            assert done.returncode == 1 and expect in out and "nothing was created, replaced or repaired" in out, (setup, out)
        else:
            assert done.returncode == 0 and "CHECK: the Stick holds this installation's key" in out and "NOT pairing" in out, out
        assert lab.writes() == [] and lab.mac_key_text() == mac_before and not (lab.flash / KEY_PART).exists(), setup
        assert "may write" not in out and lab.setup_records("device-key") == []
        no_key_leak(lab, out)


# ---- auto-start ---------------------------------------------------------------------------------------------------------------
def ready_for_autostart(lab):
    k = random_key()
    lab.mac_key(k)
    (lab.flash / KEY_FILE).write_text(k + "\n")
    lab.server.restart()
    lab.set(nvs=dict(lab.nvs(), boot_option=1))


@lab_case(key=None)
def autostart_comes_last_and_names_what_is_missing(lab):
    lab.set(nvs={"boot_option": 1})
    (lab.flash / "main.py").unlink()
    done = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--on", "--backup", str(lab.backup()))
    out = out_of(done)
    assert done.returncode == 1 and "auto-start comes last" in out and "installed files" in out and "Wi-Fi is not saved" in out
    assert "device key" in out and lab.nvs_writes() == [] and lab.nvs()["boot_option"] == 1, out


@lab_case(key=None)
def autostart_shows_the_value_needs_a_backup_sets_reads_back_and_keeps_the_previous_value(lab):
    ready_for_autostart(lab)
    refused = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--on")
    assert refused.returncode == 1 and "boot_option is now 1" in refused.stdout and "backup" in refused.stderr and lab.nvs_writes() == []
    done = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--on", "--backup", str(lab.backup()))
    out = out_of(done)
    assert done.returncode == 0 and "SET but NOT verified" in out and "File backups do not contain this setting" in out, out
    assert lab.nvs()["boot_option"] == 0 and lab.nvs_writes() == [["NVS-SET", "boot_option", 0]]
    [rec] = lab.setup_records("auto-start")
    assert rec["boot_option_before"] == 1 and rec["boot_option_after"] == 0 and rec["status"] == "complete"
    assert lab.nvs()["ssid0"] == tb.SSID and lab.nvs()["pswd0"] == tb.PASSWORD          # Wi-Fi untouched
    again = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--on")
    assert again.returncode == 0 and "Nothing to do" in again.stdout and len(lab.nvs_writes()) == 1
    off = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--off", "--backup", str(lab.backup()))
    assert off.returncode == 0 and lab.nvs()["boot_option"] == 1 and (lab.flash / "main.py").exists(), out_of(off)
    no_key_leak(lab, out, out_of(off))


@lab_case(key=None)
def autostart_check_reads_the_value_and_never_writes_it(lab):
    folder = lab.backup()
    for value, code, text in ((1, 1, "auto-start is OFF"), (0, 0, "auto-start is ON")):
        lab.set(nvs=dict(lab.nvs(), boot_option=value))
        done = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--check", "--backup", str(folder))
        assert done.returncode == code and text in done.stdout and f"boot_option is now {value}" in done.stdout, out_of(done)
        assert "OBSERVED" not in done.stdout and "may write" not in done.stdout
    assert lab.nvs_writes() == [] and lab.setup_records("auto-start") == [] and lab.nvs()["boot_option"] == 0


@lab_case(key=None, faults={"nvs_set_fail": True})
def a_failed_setting_write_is_reported_with_the_previous_value(lab):
    ready_for_autostart(lab)
    done = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--on", "--backup", str(lab.backup()))
    out = out_of(done)
    assert done.returncode == 1 and "did not take" in out and "reads back 1" in out and "previous value was 1" in out, out
    [rec] = lab.setup_records("auto-start")
    assert rec["status"] == "failed" and rec["boot_option_before"] == 1 and lab.nvs()["boot_option"] == 1


@lab_case(key=None, faults={"nvs_commit_lost": True})
def a_setting_that_does_not_persist_is_never_reported_as_set(lab):
    ready_for_autostart(lab)
    done = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--on", "--backup", str(lab.backup()))
    assert done.returncode == 1 and "did not take" in out_of(done) and "SET but" not in out_of(done), out_of(done)


# ---- pairing and the observed boot --------------------------------------------------------------------------------------------
@lab_case(key=None, server=False)
def verify_needs_the_server_and_restarts_nothing_without_it(lab):
    lab.mac_key(random_key())
    done = lab.setup("verify", "--port", lab.port, "--expect-device", device())
    assert done.returncode == 2 and "could not be read" in done.stderr and lab.calls() == [] and lab.state().get("resets", 0) == 0


@lab_case(key=None)
def a_key_the_server_has_not_loaded_is_named_and_a_restart_of_the_server_fixes_it(lab):
    folder = lab.backup()
    lab.set(nvs=dict(lab.nvs(), boot_option=0))
    assert lab.setup("key", "--port", lab.port, "--expect-device", device(), "--backup", str(folder)).returncode == 0
    refused = lab.setup("verify", "--port", lab.port, "--expect-device", device())
    assert refused.returncode == 2 and "restart the server" in refused.stderr and lab.state().get("resets", 0) == 0, out_of(refused)
    lab.server.restart()                                                        # the owner restarts the (isolated) server
    done = lab.setup("verify", "--port", lab.port, "--expect-device", device(), "--timeout", "30")
    out = out_of(done)
    assert done.returncode == 0 and "Authenticated server contact: CONFIRMED" in out and "Auto-start:                   OBSERVED" in out, out
    assert lab.state()["resets"] == 1
    [rec] = lab.setup_records("verification")
    assert rec["paired"] is True and rec["autostart_observed"] is True
    no_key_leak(lab, out)


@lab_case(key=None, wifi_result="wrong_password")
def no_report_after_a_restart_is_not_confirmed_and_says_what_is_unknown(lab):
    k = random_key()
    lab.mac_key(k)
    (lab.flash / KEY_FILE).write_text(k + "\n")
    lab.server.restart()
    body = {"proto": 1, "store": "", "rev": 0, "notif": 0, "boot": "running-before", "uptime": 3}
    assert lab.server.link.sync(k, json.dumps(body).encode(), "192.0.2.30")[0] == 200    # a fresh report from the boot BEFORE the restart
    done = lab.setup("verify", "--port", lab.port, "--expect-device", device(), "--timeout", "4")
    out = out_of(done)
    assert done.returncode == 1 and "NOT confirmed" in out and "Unknown:" in out and "`wifi`" in out, out
    assert "CONFIRMED" not in out.replace("NOT confirmed", "")
    [rec] = lab.setup_records("verification")
    assert rec["paired"] is False


@lab_case(key=None)
def a_stick_with_another_key_is_never_restarted_by_verify(lab):
    lab.mac_key(random_key())
    (lab.flash / KEY_FILE).write_text(random_key() + "\n")
    lab.server.restart()
    done = lab.setup("verify", "--port", lab.port, "--expect-device", device())
    assert done.returncode == 1 and "does not hold this installation's device key" in done.stderr and lab.state().get("resets", 0) == 0


@lab_case(key=None)
def a_running_assistant_without_auto_start_is_not_called_observed(lab):
    ready_for_autostart(lab)                                                    # boot_option 1: the menu, not the app
    done = lab.setup("verify", "--port", lab.port, "--expect-device", device(), "--timeout", "4")
    out = out_of(done)
    assert done.returncode == 1 and "boot_option is 1 (not 0)" in out and "OBSERVED" not in out, out


# ---- the whole order on a fresh Stick, and the way back after button A ---------------------------------------------------------
@lab_case(scenario="fresh", key=None)
def a_fresh_stick_goes_through_the_whole_order(lab):
    lab.set(nvs={"boot_option": 1, "ssid0": tb.SSID, "pswd0": tb.PASSWORD})          # Wi-Fi saved on the Stick with UIFlow Setup
    folder = lab.backup()
    install = lab.install("apply", "--port", lab.port, "--backup", str(folder), "--expect-device", device())
    assert install.returncode == 0, out_of(install)
    assert lab.setup("key", "--port", lab.port, "--expect-device", device(), "--backup", str(folder)).returncode == 0
    lab.server.restart()
    on = lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--on", "--backup", str(folder))
    assert on.returncode == 0, out_of(on)
    done = lab.setup("verify", "--port", lab.port, "--expect-device", device(), "--timeout", "30")
    assert done.returncode == 0 and "OBSERVED" in done.stdout, out_of(done)
    status = out_of(lab.setup("status", "--port", lab.port))
    assert "same key yes" in status and "boot_option 0" in status, status
    no_key_leak(lab, out_of(install), out_of(on), out_of(done), status)


@lab_case(key=None)
def after_button_a_removed_main_py_the_documented_reinstall_path_restores_auto_start(lab):
    k = random_key()
    lab.mac_key(k)
    (lab.flash / KEY_FILE).write_text(k + "\n")
    lab.server.restart()
    (lab.flash / "main.py").unlink()                                            # what UIFlow 2.4.8 does when A is held at start-up
    lab.set(nvs=dict(lab.nvs(), boot_option=1))
    before = out_of(lab.setup("status", "--port", lab.port))
    assert "existing installation" in before and "install the files" in before, before
    again = lab.install("apply", "--port", lab.port, "--expect-device", device(), "--expect-changes", "/flash/main.py")
    assert again.returncode == 0, out_of(again)
    assert lab.setup("autostart", "--port", lab.port, "--expect-device", device(), "--on", "--backup", str(lab.backup())).returncode == 0
    done = lab.setup("verify", "--port", lab.port, "--expect-device", device(), "--timeout", "30")
    assert done.returncode == 0 and "OBSERVED" in done.stdout, out_of(done)


# ---- neighbours ---------------------------------------------------------------------------------------------------------------
@lab_case(key=None)
def an_installation_record_keeps_the_installers_own_lists(lab):
    done = lab.install("apply", "--port", lab.port)
    assert done.returncode == 0, out_of(done)
    [path] = list((lab.data / "stick-installs").rglob("record.json"))
    rec = json.loads(path.read_text())
    assert rec["never"][0].startswith("Firmware, bootloader") and rec["limits"][0].startswith("A multi-file installation"), rec["never"]



if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:900])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:900])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
