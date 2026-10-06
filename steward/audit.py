"""Append-only, hash-chained audit log + content-addressed evidence store.

Every event's hash covers the previous event's hash, so editing or deleting any
row in the middle breaks the chain and `verify()` reports where. (Truncating the
*tail* is not detectable without an external anchor; see docs/ARCHITECTURE.md.)
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import Store, new_id, now, row_to_dict

GENESIS = "0" * 64


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev: str, task_id: str | None, ts: float, kind: str, actor: str, payload: str) -> str:
    raw = "|".join([prev, task_id or "", repr(ts), kind, actor, payload])
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    checked: int
    broken_at_seq: int | None = None
    reason: str | None = None


class AuditLog:
    def __init__(self, store: Store):
        self.store = store

    def append(self, task_id: str | None, kind: str, actor: str, payload: dict | None = None) -> dict:
        pj = canonical(payload or {})
        with self.store.tx() as c:
            row = c.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            ts = now()
            h = _hash(prev, task_id, ts, kind, actor, pj)
            cur = c.execute(
                "INSERT INTO events(task_id, ts, kind, actor, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (task_id, ts, kind, actor, pj, prev, h),
            )
            seq = cur.lastrowid
        return {"seq": seq, "task_id": task_id, "ts": ts, "kind": kind, "actor": actor,
                "payload": payload or {}, "hash": h}

    def events(self, task_id: str | None = None, kinds: list[str] | None = None,
               after_seq: int = 0, limit: int = 1000) -> list[dict]:
        sql, params = "SELECT * FROM events WHERE seq > ?", [after_seq]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit)
        out = []
        for r in self.store.query(sql, tuple(params)):
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def verify(self) -> VerifyResult:
        prev, n = GENESIS, 0
        for r in self.store.query("SELECT * FROM events ORDER BY seq"):
            if r["prev_hash"] != prev:
                return VerifyResult(False, n, r["seq"], "prev_hash does not match previous event (row deleted or reordered)")
            if _hash(prev, r["task_id"], r["ts"], r["kind"], r["actor"], r["payload"]) != r["hash"]:
                return VerifyResult(False, n, r["seq"], "event content does not match its hash (row modified)")
            prev, n = r["hash"], n + 1
        return VerifyResult(True, n)


class EvidenceStore:
    """Files (screenshots, DOM snapshots, reports) saved per task with sha256."""

    def __init__(self, store: Store, audit: AuditLog, root: str | Path):
        self.store, self.audit, self.root = store, audit, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"

    def save(self, task_id: str, name: str, data: bytes | str, kind: str = "file",
             meta: dict | None = None, actor: str = "system") -> dict:
        raw = data.encode() if isinstance(data, str) else data
        ev_id = new_id("ev")
        d = self.root / task_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{ev_id}_{self._safe(name)}"
        path.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO evidence(id, task_id, name, kind, path, sha256, size, meta, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ev_id, task_id, name, kind, str(path), sha, len(raw), canonical(meta or {}), now()),
            )
        self.audit.append(task_id, "evidence.added", actor,
                          {"evidence_id": ev_id, "name": name, "kind": kind, "sha256": sha, "size": len(raw)})
        return {"id": ev_id, "name": name, "kind": kind, "path": str(path), "sha256": sha, "size": len(raw)}

    def list(self, task_id: str) -> list[dict]:
        return [row_to_dict(r) for r in
                self.store.query("SELECT * FROM evidence WHERE task_id=? ORDER BY created_at", (task_id,))]

    def verify(self, task_id: str | None = None) -> list[dict]:
        """Return a list of problems (empty == all evidence intact)."""
        sql, params = "SELECT * FROM evidence", ()
        if task_id:
            sql, params = sql + " WHERE task_id=?", (task_id,)
        bad = []
        for r in self.store.query(sql, params):
            p = Path(r["path"])
            if not p.exists():
                bad.append({"id": r["id"], "problem": "missing"})
            elif hashlib.sha256(p.read_bytes()).hexdigest() != r["sha256"]:
                bad.append({"id": r["id"], "problem": "hash mismatch"})
        return bad
