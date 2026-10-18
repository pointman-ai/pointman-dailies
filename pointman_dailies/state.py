"""What the extension has seen and published, in its own state folder, so restarts don't republish
everything. The same tables as the Python node's state.db (agent/state.py), so taking that over is a
copy of rows (take_over)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    status TEXT NOT NULL,          -- seen | queued | processing | published | failed | skipped
    asset_id INTEGER,
    version_id INTEGER,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS note_sync (
    note_id INTEGER PRIMARY KEY,
    updated_at REAL NOT NULL,
    info TEXT
);
"""
COLUMNS = "path, size, mtime, status, asset_id, version_id, error, attempts, updated_at"


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def execute(self, sql: str, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ kv

    def get(self, key: str, default: str | None = None) -> str | None:
        rows = self.execute("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value) -> None:
        self.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))

    # ------------------------------------------------------------ files

    def file(self, path: str) -> sqlite3.Row | None:
        rows = self.execute("SELECT * FROM files WHERE path = ?", (path,))
        return rows[0] if rows else None

    def all_files(self) -> dict[str, sqlite3.Row]:
        return {r["path"]: r for r in self.execute("SELECT * FROM files")}

    def mark(self, path: str, size: int, mtime: float, status: str, *, asset_id=None, version_id=None,
             error=None, bump_attempts: bool = False) -> None:
        self.execute("""
            INSERT INTO files (path, size, mtime, status, asset_id, version_id, error, attempts, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, status = excluded.status,
                asset_id = COALESCE(excluded.asset_id, files.asset_id),
                version_id = COALESCE(excluded.version_id, files.version_id),
                error = excluded.error,
                attempts = CASE WHEN ? THEN files.attempts + 1 WHEN excluded.status = 'published' THEN 0
                                ELSE files.attempts END,
                updated_at = excluded.updated_at""",
            (path, size, mtime, status, asset_id, version_id, error, 1 if bump_attempts else 0, time.time(),
             bump_attempts))

    def mark_many_seen(self, entries: list[tuple[str, int, float]]) -> None:
        t = time.time()
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.executemany(
                "INSERT OR IGNORE INTO files (path, size, mtime, status, updated_at) VALUES (?, ?, ?, 'seen', ?)",
                [(p, s, m, t) for p, s, m in entries])
            self._conn.execute("COMMIT")

    def counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.execute("SELECT status, COUNT(*) n FROM files GROUP BY status")}

    def failures(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.execute(
            "SELECT path, error, attempts, updated_at FROM files WHERE status = 'failed' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------ notes

    def note_state(self, note_id: int) -> sqlite3.Row | None:
        rows = self.execute("SELECT * FROM note_sync WHERE note_id = ?", (note_id,))
        return rows[0] if rows else None

    def set_note_state(self, note_id: int, updated_at: float, info: str | None) -> None:
        self.execute("""INSERT INTO note_sync (note_id, updated_at, info) VALUES (?, ?, ?)
                        ON CONFLICT(note_id) DO UPDATE SET updated_at = excluded.updated_at, info = excluded.info""",
                     (note_id, updated_at, info))

    # ------------------------------------------------------------ the Python node's

    def take_over(self, old: Path) -> dict:
        """What the Python node saw and published (its state.db), merged in: each file the node has a
        newer row for than ours, its baseline if we have none, the latest time each sequence was
        published, and the review notes it wrote out. Read only, so the node can go on running. Safe on every
        start: once the node stops publishing, its rows stop changing and nothing more comes over."""
        if not old.is_file():
            return {"from": str(old), "files": 0, "found": False}
        src = sqlite3.connect(f"file:{old}?mode=ro", uri=True, timeout=30)
        src.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            rows = src.execute(f"SELECT {COLUMNS} FROM files").fetchall() if "files" in tables else []
            kv = dict(src.execute("SELECT key, value FROM kv WHERE key IN ('baseline', 'notes_since') OR key LIKE 'sequence_published:%'")
                      .fetchall()) if "kv" in tables else {}
            notes = src.execute("SELECT note_id, updated_at, info FROM note_sync").fetchall() \
                if "note_sync" in tables else []
        finally:
            src.close()
        with self._lock:
            self._conn.execute("BEGIN")
            before = self._conn.total_changes
            self._conn.executemany(f"""
                INSERT INTO files ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, status = excluded.status,
                    asset_id = excluded.asset_id, version_id = excluded.version_id, error = excluded.error,
                    attempts = excluded.attempts, updated_at = excluded.updated_at
                WHERE excluded.updated_at > files.updated_at""", [tuple(r) for r in rows])
            taken = self._conn.total_changes - before
            if "baseline" in kv:
                self._conn.execute("INSERT OR IGNORE INTO kv (key, value) VALUES ('baseline', ?)", (kv.pop("baseline"),))
            for key, value in kv.items():
                self._conn.execute("""INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE
                                      SET value = excluded.value WHERE CAST(excluded.value AS REAL) > CAST(kv.value AS REAL)""",
                                   (key, value))
            self._conn.executemany("""INSERT INTO note_sync (note_id, updated_at, info) VALUES (?, ?, ?)
                                      ON CONFLICT(note_id) DO UPDATE SET updated_at = excluded.updated_at, info = excluded.info
                                      WHERE excluded.updated_at > note_sync.updated_at""", [tuple(n) for n in notes])
            self._conn.execute("COMMIT")
        return {"from": str(old), "files": taken, "found": True}
