"""SQLite persistence for cameras, upload destinations, upload history and settings."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    record_mode TEXT NOT NULL DEFAULT 'continuous',
    schedule TEXT NOT NULL DEFAULT '[]',
    segment_seconds INTEGER NOT NULL DEFAULT 300,
    audio INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS destinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    target TEXT NOT NULL,
    cron TEXT NOT NULL DEFAULT '0 * * * *',
    camera_ids TEXT,
    delete_after_upload INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,
    last_status TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    destination_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    remote_path TEXT,
    size INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (destination_id, rel_path)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "retention_days": 14,
    "max_storage_gb": 0,  # 0 = unlimited
    "retention_require_upload": False,
    "live_fps": 5,
    "live_width": 960,
}

CAMERA_FIELDS = ("name", "url", "enabled", "record_mode", "schedule", "segment_seconds", "audio")
DEST_FIELDS = ("name", "type", "target", "cron", "camera_ids", "delete_after_upload", "enabled")
JSON_FIELDS = {"schedule", "camera_ids"}
BOOL_FIELDS = {"enabled", "audio", "delete_after_upload"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _decode(row):
        if row is None:
            return None
        d = dict(row)
        for k in JSON_FIELDS & d.keys():
            d[k] = json.loads(d[k]) if d[k] is not None else None
        for k in BOOL_FIELDS & d.keys():
            d[k] = bool(d[k])
        return d

    @staticmethod
    def _encode(data: dict, fields) -> dict:
        out = {}
        for k in fields:
            if k not in data:
                continue
            v = data[k]
            if k in JSON_FIELDS:
                v = json.dumps(v) if v is not None else None
            elif k in BOOL_FIELDS:
                v = int(bool(v))
            out[k] = v
        return out

    def _insert(self, table, data, fields):
        row = self._encode(data, fields)
        row["created_at"] = now_iso()
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
            return cur.lastrowid

    def _update(self, table, id_, data, fields):
        row = self._encode(data, fields)
        if not row:
            return
        sets = ", ".join(f"{k} = ?" for k in row)
        with self.conn() as c:
            c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*row.values(), id_])

    # -- cameras -----------------------------------------------------------
    def list_cameras(self):
        with self.conn() as c:
            return [self._decode(r) for r in c.execute("SELECT * FROM cameras ORDER BY id")]

    def get_camera(self, cam_id):
        with self.conn() as c:
            return self._decode(c.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)).fetchone())

    def create_camera(self, data):
        return self._insert("cameras", data, CAMERA_FIELDS)

    def update_camera(self, cam_id, data):
        self._update("cameras", cam_id, data, CAMERA_FIELDS)

    def delete_camera(self, cam_id):
        with self.conn() as c:
            c.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))

    # -- destinations ------------------------------------------------------
    def list_destinations(self):
        with self.conn() as c:
            return [self._decode(r) for r in c.execute("SELECT * FROM destinations ORDER BY id")]

    def get_destination(self, dest_id):
        with self.conn() as c:
            return self._decode(c.execute("SELECT * FROM destinations WHERE id = ?", (dest_id,)).fetchone())

    def create_destination(self, data):
        return self._insert("destinations", data, DEST_FIELDS)

    def update_destination(self, dest_id, data):
        self._update("destinations", dest_id, data, DEST_FIELDS)

    def set_destination_result(self, dest_id, status):
        with self.conn() as c:
            c.execute("UPDATE destinations SET last_run = ?, last_status = ? WHERE id = ?",
                      (now_iso(), status, dest_id))

    def delete_destination(self, dest_id):
        with self.conn() as c:
            c.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
            c.execute("DELETE FROM uploads WHERE destination_id = ?", (dest_id,))

    # -- uploads -----------------------------------------------------------
    def record_upload(self, dest_id, rel_path, status, size=None, remote_path=None, error=None):
        with self.conn() as c:
            c.execute(
                """INSERT INTO uploads (destination_id, rel_path, remote_path, size, status, error, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (destination_id, rel_path) DO UPDATE SET
                     remote_path = excluded.remote_path, size = excluded.size,
                     status = excluded.status, error = excluded.error, updated_at = excluded.updated_at""",
                (dest_id, rel_path, remote_path, size, status, error, now_iso()),
            )

    def uploaded_paths(self, dest_id) -> set[str]:
        with self.conn() as c:
            rows = c.execute("SELECT rel_path FROM uploads WHERE destination_id = ? AND status = 'done'", (dest_id,))
            return {r[0] for r in rows}

    def upload_map(self) -> dict[str, list[int]]:
        """rel_path -> destination ids it has been uploaded to."""
        out: dict[str, list[int]] = {}
        with self.conn() as c:
            for r in c.execute("SELECT rel_path, destination_id FROM uploads WHERE status = 'done'"):
                out.setdefault(r[0], []).append(r[1])
        return out

    def recent_uploads(self, limit=100):
        with self.conn() as c:
            rows = c.execute(
                """SELECT u.*, d.name AS destination_name FROM uploads u
                   LEFT JOIN destinations d ON d.id = u.destination_id
                   ORDER BY u.updated_at DESC, u.id DESC LIMIT ?""", (limit,))
            return [dict(r) for r in rows]

    # -- settings ----------------------------------------------------------
    def get_settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        with self.conn() as c:
            for r in c.execute("SELECT key, value FROM settings"):
                s[r[0]] = json.loads(r[1])
        return s

    def update_settings(self, data: dict):
        with self.conn() as c:
            for k, v in data.items():
                if k in DEFAULT_SETTINGS:
                    c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                              "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))
