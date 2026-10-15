"""SQLite persistence for the job queue (and Pro users/sessions).

WAL mode, short-lived connections (safe across worker threads without a
shared-connection lock), UTC ISO timestamps. This is the only state store —
no Redis, no external services.

Schema versioning via ``PRAGMA user_version``; :func:`migrate` upgrades
databases created by older Batchroom versions in place.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3

# SET clause re-deriving a job's counters from its items (jobs.id in scope).
COUNTERS_FROM_ITEMS = (
    "done = (SELECT count(*) FROM items WHERE job_id = jobs.id AND status = 'done'),"
    " failed = (SELECT count(*) FROM items WHERE job_id = jobs.id AND status = 'failed'),"
    " cancelled = (SELECT count(*) FROM items WHERE job_id = jobs.id AND status = 'cancelled')"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    recipe      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|cancelled
    source      TEXT NOT NULL DEFAULT 'api',     -- api|watch|cli
    output_dir  TEXT NOT NULL,
    webhook_url TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    cancelled   INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,      -- non-image files ignored at intake
    error       TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    priority    INTEGER NOT NULL DEFAULT 0,      -- pro: higher first
    user_id     INTEGER                           -- pro: submitting user (NULL = system)
);

CREATE TABLE IF NOT EXISTS items (
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    source_name TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|cancelled
    error       TEXT,
    result_json TEXT,
    started_at  TEXT,
    finished_at TEXT,
    PRIMARY KEY (job_id, idx)
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL DEFAULT '',
    password      TEXT NOT NULL,                 -- scrypt$N$r$p$salt$hash
    role          TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    api_key_hash  TEXT UNIQUE,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_seen   TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_pending ON items(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema in place. Idempotent."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    has_jobs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if has_jobs:
        # v0.1.0 databases predate user_version stamping: add what's missing.
        columns = _column_names(conn, "jobs")
        if "priority" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        if "user_id" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN user_id INTEGER")
    conn.executescript(SCHEMA)
    if has_jobs and version < 3:
        # v0.1.0 did not count items cancelled while still pending, so
        # finished cancelled jobs showed e.g. "2/30" forever. Repair history.
        conn.execute(f"UPDATE jobs SET {COUNTERS_FROM_ITEMS} WHERE finished_at IS NOT NULL")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            migrate(conn)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def row_to_job(row: sqlite3.Row) -> dict:
    job = dict(row)
    job.pop("cancel_requested", None)
    return job


def row_to_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    raw = item.pop("result_json", None)
    item["result"] = json.loads(raw) if raw else None
    return item


def row_to_user(row: sqlite3.Row) -> dict:
    """Public user dict — never exposes password/api-key hashes."""
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "disabled": bool(row["disabled"]),
        "has_api_key": row["api_key_hash"] is not None,
        "created_at": row["created_at"],
    }
