"""Durable background task queue on SQLite.

States: queued -> running -> completed | awaiting_approval | waiting_input | needs_human | dead
 * claim() is atomic (BEGIN IMMEDIATE) so several workers can share one DB.
 * A lease + heartbeat protects against crashed workers: an expired lease is reclaimed (counts as an attempt).
 * Transient failures are retried with exponential backoff + jitter until max_attempts, then dead-lettered.
 * Paused tasks (approval/question) are NOT retried; they are re-queued when the human has decided
   (resume_ready), so waiting for a person never occupies a worker.
"""
from __future__ import annotations

import json
import random
import time
from typing import Callable

from .audit import AuditLog
from .store import Store, new_id, now

TERMINAL = {"completed", "needs_human", "dead"}
PAUSED = {"awaiting_approval", "waiting_input"}


def default_backoff(attempts: int) -> float:
    base = min(300.0, 2.0 * 2 ** (attempts - 1))
    return base * (0.75 + 0.5 * random.random())


class TaskQueue:
    def __init__(self, store: Store, audit: AuditLog, clock: Callable[[], float] = time.time,
                 backoff: Callable[[int], float] = default_backoff):
        self.store, self.audit, self.clock, self.backoff = store, audit, clock, backoff

    # ------------------------------------------------------------------ producer
    def enqueue(self, goal: str, requester: str = "unknown", *, run_at: float | None = None,
                priority: int = 0, max_attempts: int = 3) -> str:
        tid, t = new_id("task"), self.clock()
        self.store.execute(
            "INSERT INTO tasks(id, goal, requester, status, priority, max_attempts, run_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, goal, requester, "queued", priority, max_attempts, run_at or t, t, t))
        self.audit.append(tid, "task.queued", requester, {"goal": goal, "run_at": run_at or t, "priority": priority})
        return tid

    def get(self, task_id: str) -> dict | None:
        r = self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return dict(r) if r else None

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql, p = "SELECT * FROM tasks", ()
        if status:
            sql, p = sql + " WHERE status=?", (status,)
        return [dict(r) for r in self.store.query(sql + " ORDER BY created_at DESC LIMIT ?", p + (limit,))]

    # ------------------------------------------------------------------ worker side
    def claim(self, worker_id: str, lease_s: float = 120.0) -> dict | None:
        t = self.clock()
        with self.store.tx() as c:
            # crashed worker: lease expired while 'running' -> back to queued, counts as an attempt
            for r in c.execute("SELECT id, attempts, max_attempts FROM tasks WHERE status='running' AND lease_expires < ?", (t,)).fetchall():
                attempts = r["attempts"] + 1
                if attempts >= r["max_attempts"]:
                    c.execute("UPDATE tasks SET status='dead', attempts=?, error='lease expired too many times (worker crashed?)', lease_owner=NULL, updated_at=? WHERE id=?",
                              (attempts, t, r["id"]))
                else:
                    c.execute("UPDATE tasks SET status='queued', attempts=?, lease_owner=NULL, run_at=?, updated_at=? WHERE id=?", (attempts, t, t, r["id"]))
            row = c.execute("SELECT * FROM tasks WHERE status='queued' AND run_at <= ? ORDER BY priority DESC, run_at LIMIT 1", (t,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE tasks SET status='running', lease_owner=?, lease_expires=?, updated_at=? WHERE id=?", (worker_id, t + lease_s, t, row["id"]))
        self.audit.append(row["id"], "task.claimed", worker_id, {"attempt": row["attempts"] + 1})
        return dict(row)

    def heartbeat(self, task_id: str, worker_id: str, lease_s: float = 120.0) -> bool:
        n = self.store.execute("UPDATE tasks SET lease_expires=? WHERE id=? AND lease_owner=? AND status='running'",
                               (self.clock() + lease_s, task_id, worker_id))
        return n == 1

    def finish(self, task_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
        """status: completed | awaiting_approval | waiting_input | needs_human | dead"""
        self.store.execute("UPDATE tasks SET status=?, result=?, error=?, lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE id=?",
                           (status, json.dumps(result, default=str) if result is not None else None, error, self.clock(), task_id))
        self.audit.append(task_id, f"task.{status}", "queue", {"error": error} if error else {})

    def fail_transient(self, task_id: str, error: str) -> str:
        """Schedule a retry with backoff, or dead-letter when attempts are exhausted. Returns new status."""
        row = self.get(task_id)
        attempts = row["attempts"] + 1
        t = self.clock()
        if attempts >= row["max_attempts"]:
            self.store.execute("UPDATE tasks SET status='dead', attempts=?, error=?, lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE id=?",
                               (attempts, error, t, task_id))
            self.audit.append(task_id, "task.dead", "queue", {"error": error, "attempts": attempts})
            return "dead"
        delay = self.backoff(attempts)
        self.store.execute("UPDATE tasks SET status='queued', attempts=?, error=?, run_at=?, lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE id=?",
                           (attempts, error, t + delay, t, task_id))
        self.audit.append(task_id, "task.retry_scheduled", "queue", {"error": error, "attempt": attempts, "retry_in_s": round(delay, 2)})
        return "queued"

    def defer(self, task_id: str, error: str, delay_s: float) -> None:
        """Park the task until later (e.g. the plan quota resets) WITHOUT counting an attempt: nothing is wrong with the task."""
        t = self.clock()
        self.store.execute("UPDATE tasks SET status='queued', error=?, run_at=?, lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE id=?",
                           (error, t + delay_s, t, task_id))
        self.audit.append(task_id, "task.deferred", "queue", {"error": error, "retry_in_s": round(delay_s), "reason": "quota"})

    # ------------------------------------------------------------------ humans -> queue
    def resume_ready(self) -> list[str]:
        """Re-queue paused tasks whose approvals have all been decided. Called by workers every poll."""
        resumed = []
        for r in self.store.query("SELECT id FROM tasks WHERE status='awaiting_approval'"):
            pend = self.store.one("SELECT COUNT(*) n FROM approvals WHERE task_id=? AND status='pending'", (r["id"],))["n"]
            if pend == 0:
                self.requeue(r["id"], "approval decided")
                resumed.append(r["id"])
        return resumed

    def requeue(self, task_id: str, why: str) -> None:
        self.store.execute("UPDATE tasks SET status='queued', run_at=?, updated_at=? WHERE id=?", (self.clock(), self.clock(), task_id))
        self.audit.append(task_id, "task.requeued", "queue", {"why": why})
