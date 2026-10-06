"""Company memory: static Company Pack + learned lessons + per-task working notes.

Retrieval is deliberately simple (BM25 over small, curated text). The interface
(`search`) is what the runtime depends on, so embeddings can replace it later.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .store import Store, new_id, now

_WORD = re.compile(r"[a-z0-9_@.\-]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class BM25Okapi:
    """Tiny BM25 with smoothed IDF (log(1 + ...)), so it stays positive on very small corpora.
    (rank_bm25's classic IDF goes to 0/negative with 2-3 documents, which is our normal size.)"""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.n = len(docs)
        self.avgdl = (sum(len(d) for d in docs) / self.n) if self.n else 0.0
        df: dict[str, int] = {}
        for d in docs:
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def get_scores(self, query: list[str]) -> list[float]:
        out = []
        for d in self.docs:
            tf: dict[str, int] = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for q in query:
                if q in tf:
                    f = tf[q]
                    score += self.idf[q] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / (self.avgdl or 1)))
            out.append(score)
        return out


@dataclass
class SOP:
    id: str
    title: str
    triggers: list[str]
    systems: list[str]
    body: str
    done_when: list[str] = field(default_factory=list)   # success criteria the verifier must confirm
    tools: list[str] | None = None                       # optional tool-glob narrowing (least privilege)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "triggers": self.triggers, "systems": self.systems}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body.strip()
    return {}, text.strip()


class CompanyPack:
    """Everything Steward knows about a company before it has done any work there."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.policies = self._yaml("policies.yaml")
        self.systems: dict = self._yaml("systems.yaml").get("systems", {})
        self.directory: dict = self._yaml("directory.yaml")
        self.sops: dict[str, SOP] = {}
        for p in sorted((self.root / "sops").glob("*.md")) if (self.root / "sops").exists() else []:
            meta, body = _parse_frontmatter(p.read_text())
            sid = meta.get("id", p.stem)
            self.sops[sid] = SOP(sid, meta.get("title", sid), meta.get("triggers", []), meta.get("systems", []),
                                 body, meta.get("done_when", []), meta.get("tools"))

    def _yaml(self, name: str) -> dict:
        p = self.root / name
        return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}

    # -- retrieval ---------------------------------------------------------
    def sop_index(self) -> list[dict]:
        return [s.summary() for s in self.sops.values()]

    def search_sops(self, query: str, k: int = 3) -> list[tuple[SOP, float]]:
        sops = list(self.sops.values())
        if not sops:
            return []
        docs = [tokenize(" ".join([s.title, " ".join(s.triggers), " ".join(s.systems), s.body])) for s in sops]
        scores = BM25Okapi(docs).get_scores(tokenize(query))
        ranked = sorted(zip(sops, scores), key=lambda x: -x[1])
        return [(s, float(sc)) for s, sc in ranked[:k] if sc > 0]

    def system_info(self, names: list[str]) -> dict:
        return {n: self.systems[n] for n in names if n in self.systems}

    def lookup_person(self, query: str) -> list[dict]:
        q = query.lower()
        return [p for p in self.directory.get("people", [])
                if q in p.get("name", "").lower() or q in p.get("email", "").lower() or q in p.get("role", "").lower()]


class Memory:
    """Learned memory + working notes, backed by the shared SQLite store."""

    def __init__(self, store: Store, pack: CompanyPack):
        self.store, self.pack = store, pack

    # -- lessons (learned from humans / outcomes) --------------------------
    def add_lesson(self, text: str, *, source: str, scope: str = "global", task_id: str | None = None) -> str:
        lid = new_id("les")
        self.store.execute("INSERT INTO lessons(id, scope, text, source, task_id, created_at) VALUES (?,?,?,?,?,?)",
                           (lid, scope, text, source, task_id, now()))
        return lid

    def lessons(self, query: str, scopes: list[str] | None = None, k: int = 5) -> list[dict]:
        rows = [dict(r) for r in self.store.query("SELECT * FROM lessons")]
        if scopes is not None:
            rows = [r for r in rows if r["scope"] == "global" or r["scope"] in scopes]
        if not rows:
            return []
        scores = BM25Okapi([tokenize(r["text"]) for r in rows]).get_scores(tokenize(query))
        ranked = sorted(zip(rows, scores), key=lambda x: -x[1])
        return [r for r, sc in ranked[:k] if sc > 0]

    # -- working notes -----------------------------------------------------
    def note(self, task_id: str, key: str, value: str) -> None:
        self.store.execute(
            "INSERT INTO notes(task_id, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(task_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (task_id, key, value, now()))

    def notes(self, task_id: str) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.store.query("SELECT key, value FROM notes WHERE task_id=?", (task_id,))}

    # -- context assembly for the Understand step --------------------------
    def context_for(self, request: str, k_sops: int = 2) -> dict:
        hits = self.pack.search_sops(request, k_sops)
        systems = sorted({s for sop, _ in hits for s in sop.systems})
        return {
            "candidate_sops": [{"id": s.id, "title": s.title, "score": round(sc, 2), "body": s.body,
                                "done_when": s.done_when, "systems": s.systems} for s, sc in hits],
            "systems": self.pack.system_info(systems),
            "lessons": self.lessons(request, scopes=[f"system:{s}" for s in systems] + [f"sop:{s.id}" for s, _ in hits]),
        }
