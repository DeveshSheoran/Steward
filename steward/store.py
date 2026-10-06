"""SQLite-backed durable state: tasks (queue), audit events, approvals, evidence.

One file DB in WAL mode so API server, workers and CLI can share it across
processes. Writes go through `tx()` which uses BEGIN IMMEDIATE, so
read-modify-write sequences (audit hash chain, queue leasing) are atomic.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    goal          TEXT NOT NULL,
    requester     TEXT NOT NULL DEFAULT 'unknown',
    status        TEXT NOT NULL DEFAULT 'queued',
    spec          TEXT,
    result        TEXT,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    priority      INTEGER NOT NULL DEFAULT 0,
    run_at        REAL NOT NULL,
    lease_owner   TEXT,
    lease_expires REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_ready ON tasks(status, run_at);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    tool         TEXT NOT NULL,
    args         TEXT NOT NULL,
    risk         INTEGER NOT NULL,
    reasons      TEXT NOT NULL,
    approver     TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|consumed|expired
    decided_by   TEXT,
    comment      TEXT,
    created_at   REAL NOT NULL,
    decided_at   REAL,
    expires_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_fp ON approvals(task_id, fingerprint, status);

CREATE TABLE IF NOT EXISTS evidence (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence(task_id);

-- learned memory: lessons distilled from human feedback / outcomes (with provenance)
CREATE TABLE IF NOT EXISTS lessons (
    id         TEXT PRIMARY KEY,
    scope      TEXT NOT NULL DEFAULT 'global',   -- global | system:<name> | sop:<id>
    text       TEXT NOT NULL,
    source     TEXT NOT NULL,                    -- e.g. approval:apr_x, user:devesh
    task_id    TEXT,
    created_at REAL NOT NULL
);

-- agent run checkpoint (conversation + phase) so a task can pause, crash and resume
CREATE TABLE IF NOT EXISTS run_state (
    task_id    TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- working memory: facts the agent records during one task
CREATE TABLE IF NOT EXISTS notes (
    task_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (task_id, key)
);
"""


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)  # idempotent (IF NOT EXISTS)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.tx() as c:
            return c.execute(sql, params).rowcount

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
