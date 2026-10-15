"""SQLite storage for the test server: devices, users, command queue, punches."""

import sqlite3
import threading
import time

from . import adms

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    sn TEXT PRIMARY KEY,
    ip TEXT,
    info TEXT,
    options TEXT,
    first_seen REAL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS users (
    sn TEXT NOT NULL,
    pin TEXT NOT NULL,
    name TEXT,
    source TEXT,              -- 'app' (registered here) or 'device' (reported by device)
    updated REAL,
    PRIMARY KEY (sn, pin)
);
CREATE TABLE IF NOT EXISTS fingerprints (
    sn TEXT NOT NULL,
    pin TEXT NOT NULL,
    finger INTEGER,
    size INTEGER,
    template TEXT,            -- only filled when --store-templates is on
    received REAL,
    PRIMARY KEY (sn, pin, finger)
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sn TEXT NOT NULL,
    pin TEXT,
    kind TEXT,                -- add_user / delete_user / enroll / raw
    text TEXT NOT NULL,
    status TEXT NOT NULL,     -- queued / sent / done / failed / timeout / cancelled
    return_code INTEGER,
    created REAL,
    sent REAL,
    finished REAL
);
CREATE TABLE IF NOT EXISTS attlogs (
    sn TEXT NOT NULL,
    pin TEXT NOT NULL,
    time TEXT NOT NULL,
    status TEXT,
    verify TEXT,
    received REAL,
    PRIMARY KEY (sn, pin, time)
);
"""

# How long a sent command may go unanswered before we give up on it.
# Enrollment waits for a person to walk up and scan three times.
SENT_TIMEOUT = {"enroll": 180}
DEFAULT_SENT_TIMEOUT = 60
MAX_COMMANDS_PER_POLL = 10


class Store:
    def __init__(self, path, store_templates=False):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.store_templates = store_templates

    def _q(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ------------------------------------------------------------ devices

    def touch_device(self, sn, ip, info=None):
        now = time.time()
        self._q(
            "INSERT INTO devices (sn, ip, info, first_seen, last_seen) VALUES (?,?,?,?,?) "
            "ON CONFLICT(sn) DO UPDATE SET ip=excluded.ip, last_seen=excluded.last_seen, "
            "info=COALESCE(excluded.info, devices.info)",
            (sn, ip, info, now, now),
        )

    def set_device_options(self, sn, options):
        self._q("UPDATE devices SET options=? WHERE sn=?", (options, sn))

    def devices(self):
        return self._all("SELECT * FROM devices ORDER BY last_seen DESC")

    # ------------------------------------------------------------ users / fingerprints

    def upsert_user(self, sn, pin, name, source):
        self._q(
            "INSERT INTO users (sn, pin, name, source, updated) VALUES (?,?,?,?,?) "
            "ON CONFLICT(sn, pin) DO UPDATE SET name=excluded.name, updated=excluded.updated",
            (sn, pin, name, source, time.time()),
        )

    def delete_user(self, sn, pin):
        self._q("DELETE FROM users WHERE sn=? AND pin=?", (sn, pin))
        self._q("DELETE FROM fingerprints WHERE sn=? AND pin=?", (sn, pin))

    def add_fingerprint(self, sn, fp):
        self._q(
            "INSERT OR REPLACE INTO fingerprints (sn, pin, finger, size, template, received) "
            "VALUES (?,?,?,?,?,?)",
            (sn, fp["pin"], fp["finger"], fp["size"],
             fp["template"] if self.store_templates else None, time.time()),
        )

    def users(self, sn):
        return self._all(
            "SELECT u.pin, u.name, u.source, u.updated, "
            "  (SELECT COUNT(*) FROM fingerprints f WHERE f.sn=u.sn AND f.pin=u.pin) AS fingers "
            "FROM users u WHERE u.sn=? ORDER BY u.updated DESC",
            (sn,),
        )

    def fingerprints_for(self, sn, pin):
        return self._all(
            "SELECT finger, size, received FROM fingerprints WHERE sn=? AND pin=?", (sn, pin)
        )

    # ------------------------------------------------------------ attendance

    def add_attlogs(self, sn, rows):
        now = time.time()
        with self.lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO attlogs (sn, pin, time, status, verify, received) "
                "VALUES (?,?,?,?,?,?)",
                [(sn, r["pin"], r["time"], r["status"], r["verify"], now) for r in rows],
            )
            self.db.commit()

    def attlogs(self, sn, limit=50):
        return self._all(
            "SELECT a.pin, a.time, a.status, a.verify, u.name FROM attlogs a "
            "LEFT JOIN users u ON u.sn=a.sn AND u.pin=a.pin "
            "WHERE a.sn=? ORDER BY a.time DESC LIMIT ?",
            (sn, limit),
        )

    # ------------------------------------------------------------ command queue

    def queue(self, sn, kind, text, pin=None):
        cur = self._q(
            "INSERT INTO commands (sn, pin, kind, text, status, created) VALUES (?,?,?,?,?,?)",
            (sn, pin, kind, text, "queued", time.time()),
        )
        return cur.lastrowid

    def cancel(self, cid):
        cur = self._q(
            "UPDATE commands SET status='cancelled', finished=? WHERE id=? AND status='queued'",
            (time.time(), cid),
        )
        return cur.rowcount > 0

    def next_commands(self, sn):
        """Commands to hand the device on this getrequest poll.

        Commands go out strictly in order and we wait for each batch's results
        before sending more. An enrollment command is always sent on its own,
        because the device sits on the 'place finger' screen until it finishes.
        """
        now = time.time()
        with self.lock:
            in_flight = self.db.execute(
                "SELECT id, kind, sent FROM commands WHERE sn=? AND status='sent'", (sn,)
            ).fetchall()
            waiting = False
            for row in in_flight:
                limit = SENT_TIMEOUT.get(row["kind"], DEFAULT_SENT_TIMEOUT)
                if now - row["sent"] > limit:
                    self.db.execute(
                        "UPDATE commands SET status='timeout', finished=? WHERE id=?", (now, row["id"])
                    )
                else:
                    waiting = True
            if waiting:
                self.db.commit()
                return []

            queued = self.db.execute(
                "SELECT id, kind, text FROM commands WHERE sn=? AND status='queued' ORDER BY id LIMIT ?",
                (sn, MAX_COMMANDS_PER_POLL),
            ).fetchall()
            batch = []
            for row in queued:
                if adms.is_enroll_command(row["text"]) and batch:
                    break
                batch.append((row["id"], row["text"]))
                if adms.is_enroll_command(row["text"]):
                    break
            self.db.executemany(
                "UPDATE commands SET status='sent', sent=? WHERE id=?", [(now, cid) for cid, _ in batch]
            )
            self.db.commit()
            return batch

    def finish_command(self, sn, cid, return_code):
        status = "done" if return_code == 0 else "failed"
        self._q(
            "UPDATE commands SET status=?, return_code=?, finished=? WHERE id=? AND sn=?",
            (status, return_code, time.time(), cid, sn),
        )
        rows = self._all("SELECT * FROM commands WHERE id=? AND sn=?", (cid, sn))
        return rows[0] if rows else None

    def commands(self, sn, limit=50):
        return self._all(
            "SELECT * FROM commands WHERE sn=? ORDER BY id DESC LIMIT ?", (sn, limit)
        )

    def commands_for_pin(self, sn, pin):
        return self._all(
            "SELECT * FROM commands WHERE sn=? AND pin=? ORDER BY id", (sn, pin)
        )
