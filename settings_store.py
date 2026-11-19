"""SQLite storage for the desktop panel: desired settings with a revision, and the last device report.

Every operation opens its own short-lived connection and runs in one transaction, so
the store is safe to use from the HTTP threads at the same time, and a setting
change (value + revision) is atomic: it either happens completely or not at all.
"""
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 1       # bumped only for changes an older server could not tolerate
SCHEMA_MINOR = 3         # additive changes (v0.8.0: commands table, v0.9.0: reminders table); older servers ignore them
BRIGHTNESS_MIN = 5
BRIGHTNESS_MAX = 100

# Desired settings: name -> ("range", low, high) or ("choice", allowed values)
SETTINGS = {
    "brightness": ("range", BRIGHTNESS_MIN, BRIGHTNESS_MAX),
    "voice_volume": ("range", 0, 100),          # spoken answers and shake clips; 0 is mute
    "chime_volume": ("range", 0, 100),          # notification chimes; 0 is mute
    "idle_timeout_s": ("choice", (0, 30, 60, 300)),   # 0 = automatic sleep disabled
    "sleep_mode": ("choice", ("dim", "off")),   # what the screen does after the sleeping face
}
# One-time reminders (reminders.py owns the logic). occ is the durable occurrence id: it changes on every edit, so
# acknowledgements, audio files and queued deliveries of an older version can never be mistaken for the current one.
REMINDERS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, occ INTEGER NOT NULL UNIQUE, client_key TEXT UNIQUE, "
    "text TEXT NOT NULL, mode TEXT NOT NULL, due_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "status TEXT NOT NULL, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, audio TEXT NOT NULL DEFAULT 'none', "
    "audio_tries INTEGER NOT NULL DEFAULT 0, delivered_mode TEXT, sent_at REAL, sent_boot TEXT, received_at REAL, "
    "presented_at REAL, finished_at REAL)")
COMMAND_TYPES = ("sleep", "wake")
COMMAND_RESULTS = ("executed", "expired", "ignored")
OPEN_STATUSES = ("pending", "delivered")


def validate_setting(key, value):
    """Return value if it is a valid desired value for key (exact types, no booleans), else raise ValueError."""
    spec = SETTINGS.get(key)
    if spec is None:
        raise ValueError("unknown setting")
    if spec[0] == "range":
        if isinstance(value, bool) or not isinstance(value, int) or not spec[1] <= value <= spec[2]:
            raise ValueError(f"{key} must be an integer from {spec[1]} to {spec[2]}")
    elif isinstance(value, bool) or type(value) is not type(spec[1][0]) or value not in spec[1]:
        raise ValueError(f"{key} must be one of {list(spec[1])}")
    return value


class StoreError(Exception):
    """The database cannot be used (corrupt, newer than this code, unwritable)."""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            self._migrate()
        except (sqlite3.Error, OSError) as error:
            raise StoreError(f"cannot open {self.path.name}: {error}") from error

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def connect(self):
        """A new short-lived connection (autocommit; callers manage their own transactions)."""
        return self._connect()

    def _backup(self, label):
        """Consistent copy of the database next to it (sqlite backup API); kept after the migration."""
        target = self.path.with_name(f"{self.path.name}.{label}-{time.strftime('%Y%m%d_%H%M%S')}.bak")
        source = sqlite3.connect(self.path)
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        os.chmod(target, 0o600)
        return target

    def _migrate(self):
        with closing(self._connect()) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError(f"database version {version} is newer than this server supports "
                                 f"({SCHEMA_VERSION}); use the matching server version")
            minor = 0
            if version >= 1:
                row = con.execute("SELECT value FROM meta WHERE key = 'schema_minor'").fetchone()
                minor = int(row[0]) if row else 1
        if version >= 1 and minor < SCHEMA_MINOR:
            self._backup(f"pre-minor{SCHEMA_MINOR}")        # never alter an existing database without a copy
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            if version < 1:
                con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                            "updated_at REAL NOT NULL)")
                con.execute("CREATE TABLE IF NOT EXISTS device_report (id INTEGER PRIMARY KEY CHECK (id = 1), "
                            "received_at REAL NOT NULL, payload TEXT NOT NULL)")
                con.execute("INSERT OR IGNORE INTO meta VALUES ('store_id', ?)", (uuid.uuid4().hex[:16],))
                con.execute("INSERT OR IGNORE INTO meta VALUES ('rev', '0')")
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            if version < 1 or minor < SCHEMA_MINOR:
                con.execute("CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY, type TEXT NOT NULL, "
                            "created_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, "
                            "delivered_at REAL, delivered_boot TEXT, finished_at REAL, detail TEXT)")
            if version < 1 or minor < 3:
                con.execute(REMINDERS_SCHEMA)
                con.execute("CREATE INDEX IF NOT EXISTS reminders_status ON reminders (status, due_at)")
            if version < 1 or minor < SCHEMA_MINOR:
                con.execute("INSERT INTO meta VALUES ('schema_minor', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_MINOR),))
            con.execute("COMMIT")
        if os.name == "posix":
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(self.path) + suffix)
                if candidate.exists():
                    os.chmod(candidate, 0o600)

    # ---- desired settings ----------------------------------------------------------------
    def desired(self):
        """(store_id, rev, {"brightness": n} or {}, updated_at or None) read in one transaction."""
        with closing(self._connect()) as con:
            con.execute("BEGIN")
            meta = dict(con.execute("SELECT key, value FROM meta"))
            rows = con.execute("SELECT key, value, updated_at FROM settings").fetchall()
            con.execute("COMMIT")
        settings = {key: json.loads(value) for key, value, _ in rows}
        updated = max((updated for _, _, updated in rows), default=None)
        return meta["store_id"], int(meta["rev"]), settings, updated

    def set_setting(self, key, value, now=None):
        """Set a desired setting. Returns (rev, changed). Same value: no change, no new revision."""
        validate_setting(key, value)
        now = time.time() if now is None else now
        encoded = json.dumps(value)
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            rev = int(con.execute("SELECT value FROM meta WHERE key = 'rev'").fetchone()[0])
            if row is not None and json.loads(row[0]) == value and type(json.loads(row[0])) is type(value):
                con.execute("COMMIT")
                return rev, False
            rev += 1
            con.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                        (key, encoded, now))
            con.execute("UPDATE meta SET value = ? WHERE key = 'rev'", (str(rev),))
            con.execute("COMMIT")
            return rev, True

    def set_brightness(self, value, now=None):
        return self.set_setting("brightness", value, now)

    def setting_times(self):
        """key -> when it was last changed (epoch seconds)."""
        with closing(self._connect()) as con:
            return dict(con.execute("SELECT key, updated_at FROM settings").fetchall())

    # ---- one-shot commands (sleep / wake) ------------------------------------------------------
    @staticmethod
    def _command_row(row):
        keys = ("id", "type", "created_at", "expires_at", "status", "delivered_at", "delivered_boot",
                "finished_at", "detail")
        return dict(zip(keys, row))

    def _sweep(self, con, now):
        con.execute("UPDATE commands SET status = 'expired', finished_at = ?, detail = 'not executed in time' "
                    "WHERE status IN ('pending', 'delivered') AND expires_at <= ?", (now, now))

    def add_command(self, kind, ttl_s, now=None):
        """Queue a one-shot command that expires after ttl_s. It replaces any earlier unfinished one."""
        if kind not in COMMAND_TYPES:
            raise ValueError("unknown command")
        now = time.time() if now is None else now
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            last = con.execute("SELECT COALESCE(MAX(id), 0) FROM commands").fetchone()[0]
            command_id = max(last + 1, int(now * 1000))
            con.execute("UPDATE commands SET status = 'superseded', finished_at = ?, detail = 'replaced by a newer command' "
                        "WHERE status IN ('pending', 'delivered')", (now,))
            con.execute("INSERT INTO commands (id, type, created_at, expires_at, status) VALUES (?, ?, ?, ?, 'pending')",
                        (command_id, kind, now, now + ttl_s))
            con.execute("COMMIT")
        return command_id

    def next_command(self, now):
        """The unfinished, unexpired command to deliver (expired ones are retired first), or None."""
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            self._sweep(con, now)
            row = con.execute("SELECT id, type, created_at, expires_at, status, delivered_at, delivered_boot, "
                              "finished_at, detail FROM commands WHERE status IN ('pending', 'delivered') "
                              "ORDER BY id DESC LIMIT 1").fetchone()
            con.execute("COMMIT")
        return self._command_row(row) if row else None

    def mark_delivered(self, command_id, now, boot):
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE commands SET status = 'delivered', delivered_at = COALESCE(delivered_at, ?), "
                        "delivered_boot = COALESCE(delivered_boot, ?) WHERE id = ? AND status IN ('pending', 'delivered')",
                        (now, boot, command_id))
            con.execute("COMMIT")

    def finish_command(self, command_id, result, now):
        """Record what the Stick says happened. Only commands that were sent can finish; a late 'executed'
        also corrects a server-side expiry, because the Stick's report is the authority on execution."""
        if result not in COMMAND_RESULTS:
            return False
        detail = {"executed": "executed by the Stick", "expired": "expired before the Stick could run it",
                  "ignored": "ignored by the Stick"}[result]
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            cursor = con.execute("UPDATE commands SET status = ?, finished_at = ?, detail = ? "
                                 "WHERE id = ? AND delivered_at IS NOT NULL AND status IN ('delivered', 'expired')",
                                 (result, now, detail, command_id))
            con.execute("COMMIT")
            return cursor.rowcount > 0

    def fail_other_boot(self, boot, now):
        """Commands delivered to an earlier boot are lost with that boot: never replay them."""
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            cursor = con.execute("UPDATE commands SET status = 'failed', finished_at = ?, detail = 'the Stick restarted' "
                                 "WHERE status = 'delivered' AND delivered_boot != ?", (now, boot))
            con.execute("COMMIT")
            return cursor.rowcount

    def latest_command(self, now):
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            self._sweep(con, now)
            row = con.execute("SELECT id, type, created_at, expires_at, status, delivered_at, delivered_boot, "
                              "finished_at, detail FROM commands ORDER BY id DESC LIMIT 1").fetchone()
            con.execute("COMMIT")
        return self._command_row(row) if row else None

    # ---- last device report --------------------------------------------------------------
    def save_report(self, report, received_at):
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO device_report (id, received_at, payload) VALUES (1, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET received_at = excluded.received_at, payload = excluded.payload",
                        (received_at, json.dumps(report)))
            con.execute("COMMIT")

    def load_report(self):
        """(received_at, report dict) or None."""
        with closing(self._connect()) as con:
            row = con.execute("SELECT received_at, payload FROM device_report WHERE id = 1").fetchone()
        return (row[0], json.loads(row[1])) if row else None
