"""The server's state in SQLite (``<data root>/db.sqlite``).

Holds what used to live in ``leases.json`` and ``bsdp-bindings.json``: the
Macs seen so far, each Mac's saved image (its last BSDP SELECT, as Apple's
bsdpd keeps in ``/var/db/bsdpd_clients``) and the full-mode address leases.
Also the network settings last applied (``settings``, from schema 2) and
the web UI's logins (``sessions``, from schema 3).
Images stay in their folders, described by ``NBImageInfo.plist``.

WAL mode lets a CLI command (``set-image``) change a Mac's saved image while
``serve`` runs; the server reads it again on each request from that Mac.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCHEMA_VERSION = 3
DB_NAME = "db.sqlite"

_SCHEMA = [
    # version 1
    """
    CREATE TABLE clients (
        mac            TEXT PRIMARY KEY,   -- lower case, colon separated
        arch           TEXT,               -- from the BSDP vendor class: ppc, i386
        model          TEXT,               -- e.g. PowerMac2,2
        ip             TEXT,
        first_seen     REAL,
        last_seen      REAL,
        last_message   TEXT,               -- DISCOVER, INFORM, BOOTP, ...
        selected_image INTEGER             -- image index the Mac last selected
    );
    CREATE TABLE leases (
        mac     TEXT PRIMARY KEY,
        ip      TEXT NOT NULL,
        expires REAL NOT NULL
    );
    """,
    # version 2: settings changed in the web UI (JSON values)
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # version 3: web UI logins, so they survive a restart (hashes only, not tokens)
    """
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        expires    REAL NOT NULL
    );
    """,
]


@dataclass
class ClientRow:
    mac: str
    arch: Optional[str] = None
    model: Optional[str] = None
    ip: Optional[str] = None
    first_seen: Optional[float] = None
    last_seen: Optional[float] = None
    last_message: Optional[str] = None
    selected_image: Optional[int] = None


_CLIENT_FIELDS = ("arch", "model", "ip", "first_seen", "last_seen", "last_message",
                  "selected_image")


class Store:
    def __init__(self, path: Optional[Path] = None) -> None:
        """``path`` None keeps everything in memory (tests)."""
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path) if path else ":memory:",
                                   check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 3000")
        if path:
            self._db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    @classmethod
    def open(cls, data_root: Path) -> "Store":
        return cls(data_root / DB_NAME)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} has schema version {version}; this "
                                   f"simplenetinstallppc only knows up to {SCHEMA_VERSION}")
            for number in range(version, SCHEMA_VERSION):
                self._db.executescript("BEGIN;" + _SCHEMA[number] +
                                       f"PRAGMA user_version = {number + 1}; COMMIT;")

    # -- clients -------------------------------------------------------------------
    def client(self, mac: str) -> Optional[ClientRow]:
        with self._lock:
            row = self._db.execute("SELECT * FROM clients WHERE mac = ?",
                                   (mac.lower(),)).fetchone()
        return ClientRow(**dict(row)) if row else None

    def clients(self) -> List[ClientRow]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM clients ORDER BY last_seen DESC, mac"
                                    ).fetchall()
        return [ClientRow(**dict(r)) for r in rows]

    def update_client(self, mac: str, **fields) -> None:
        """Insert or update one Mac; only the given fields change (None clears)."""
        unknown = set(fields) - set(_CLIENT_FIELDS)
        if unknown:
            raise TypeError(f"unknown client fields: {sorted(unknown)}")
        mac = mac.lower()
        with self._lock:
            self._db.execute("INSERT INTO clients (mac, first_seen) VALUES (?, ?) "
                             "ON CONFLICT(mac) DO NOTHING", (mac, time.time()))
            if fields:
                assignments = ", ".join(f"{name} = ?" for name in fields)
                self._db.execute(f"UPDATE clients SET {assignments} WHERE mac = ?",
                                 (*fields.values(), mac))

    def selected_image(self, mac: str) -> Optional[int]:
        with self._lock:
            row = self._db.execute("SELECT selected_image FROM clients WHERE mac = ?",
                                   (mac.lower(),)).fetchone()
        return row[0] if row else None

    def set_selected_image(self, mac: str, index: Optional[int]) -> None:
        self.update_client(mac, selected_image=index)

    # -- leases --------------------------------------------------------------------
    def leases(self) -> Dict[str, Tuple[str, float]]:
        with self._lock:
            rows = self._db.execute("SELECT mac, ip, expires FROM leases").fetchall()
        return {r["mac"]: (r["ip"], r["expires"]) for r in rows}

    def save_lease(self, mac: str, ip: str, expires: float) -> None:
        with self._lock:
            self._db.execute("INSERT INTO leases (mac, ip, expires) VALUES (?, ?, ?) "
                             "ON CONFLICT(mac) DO UPDATE SET ip = excluded.ip, "
                             "expires = excluded.expires", (mac.lower(), ip, expires))

    def delete_lease(self, mac: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM leases WHERE mac = ?", (mac.lower(),))

    # -- settings ------------------------------------------------------------------
    def setting(self, key: str):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                             (key, json.dumps(value)))

    # -- web UI logins ---------------------------------------------------------------
    def add_session(self, token_hash: str, expires: float) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
            self._db.execute("INSERT OR REPLACE INTO sessions (token_hash, expires) VALUES (?, ?)",
                             (token_hash, expires))

    def session_expires(self, token_hash: str) -> Optional[float]:
        with self._lock:
            row = self._db.execute("SELECT expires FROM sessions WHERE token_hash = ?",
                                   (token_hash,)).fetchone()
        return row[0] if row else None

    def delete_session(self, token_hash: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    # -- the JSON files this replaces ------------------------------------------------
    def import_json(self, state_dir: Path) -> List[Tuple[int, str]]:
        """Import ``leases.json`` and ``bsdp-bindings.json`` once, renaming each to
        ``*.imported``. Returns (log level, message) per file found."""
        done: List[Tuple[int, str]] = []
        for name, load in (("leases.json", self._import_leases),
                           ("bsdp-bindings.json", self._import_bindings)):
            path = state_dir / name
            if not path.exists():
                continue
            try:
                what = load(json.loads(path.read_text()))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                done.append((30, f"could not import {path} ({exc}); left in place"))
                continue
            path.replace(path.with_name(name + ".imported"))
            done.append((20, f"imported {what} from {path} into {DB_NAME}"))
        return done

    def _import_leases(self, data: dict) -> str:
        for mac, entry in data.items():
            self.save_lease(mac, str(entry["ip"]), float(entry["expires"]))
        return f"{len(data)} lease(s)"

    def _import_bindings(self, data: dict) -> str:
        for mac, index in data.items():
            self.set_selected_image(mac, int(index))
        return f"{len(data)} saved image choice(s)"
