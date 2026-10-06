"""Generic, company-agnostic tools: files, read-only HTTP to company systems, outbox (mock mail), directory."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..audit import canonical
from .base import resolve_company_url, ErrorKind, Risk, Tool, ToolContext, ToolRegistry, ToolResult

S = {"type": "string"}
obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}


def _safe_path(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if root.resolve() not in p.parents and p != root.resolve():
        raise PermissionError(f"path escapes the allowed folder: {rel}")
    return p


def _read_table(p: Path) -> list[dict]:
    if p.suffix.lower() == ".xlsx":
        import openpyxl
        ws = openpyxl.load_workbook(p, read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        head = [str(h) for h in rows[0]]
        return [dict(zip(head, r)) for r in rows[1:]]
    return list(csv.DictReader(io.StringIO(p.read_text())))


def register_standard_tools(reg: ToolRegistry, *, cfg, pack) -> None:
    files_root = Path(cfg.files_dir)
    allowed_hosts = {urlparse(s["base_url"]).netloc for s in pack.systems.values() if s.get("base_url")}
    outbox_path = Path(cfg.data_dir) / "outbox.jsonl"

    def resolve(ctx: ToolContext, rel: str) -> Path:
        if rel.startswith("out/"):                      # files this task produced
            return _safe_path(Path(cfg.workdir) / "out" / ctx.task_id, rel[4:])
        return _safe_path(files_root, rel)

    # ---------------------------------------------------------------- files
    def files_list(ctx, a):
        d = resolve(ctx, a.get("path", "")) if a.get("path") else files_root
        if not d.exists():
            return ToolResult(False, error=f"no such folder: {a.get('path','')}", error_kind=ErrorKind.PERMANENT)
        return ToolResult(True, "\n".join(sorted(f"{x.name}{'/' if x.is_dir() else ''}" for x in d.iterdir())) or "(empty)")

    def files_read(ctx, a):
        try:
            p = resolve(ctx, a["path"])
            if not p.is_file():
                return ToolResult(False, error=f"no such file: {a['path']}", error_kind=ErrorKind.PERMANENT)
            if p.suffix.lower() in (".csv", ".xlsx"):
                rows = _read_table(p)
                return ToolResult(True, json.dumps({"columns": list(rows[0]) if rows else [], "row_count": len(rows), "rows": rows[:200]}, default=str))
            return ToolResult(True, p.read_text()[:20000])
        except PermissionError as e:
            return ToolResult(False, error=str(e), error_kind=ErrorKind.PERMANENT)

    def files_summarize(ctx, a):
        try:
            rows = _read_table(resolve(ctx, a["path"]))
            out = {"row_count": len(rows), "sums": {}}
            for c in a.get("sum_columns", []):
                out["sums"][c] = sum(float(r[c] or 0) for r in rows)
            if a.get("group_by"):
                g: dict = {}
                for r in rows:
                    k = str(r[a["group_by"]])
                    g.setdefault(k, {"count": 0, **{c: 0.0 for c in a.get("sum_columns", [])}})
                    g[k]["count"] += 1
                    for c in a.get("sum_columns", []):
                        g[k][c] += float(r[c] or 0)
                out["by_" + a["group_by"]] = g
            return ToolResult(True, json.dumps(out))
        except (KeyError, ValueError, PermissionError) as e:
            return ToolResult(False, error=f"{type(e).__name__}: {e}", error_kind=ErrorKind.PERMANENT)

    def files_write(ctx, a):
        try:
            p = _safe_path(Path(cfg.workdir) / "out" / ctx.task_id, a["path"])
        except PermissionError as e:
            return ToolResult(False, error=str(e), error_kind=ErrorKind.PERMANENT)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(a["content"])
        ev = ctx.evidence.save(ctx.task_id, a["path"], a["content"], kind="output", actor="agent")
        return ToolResult(True, f"wrote out/{a['path']} ({len(a['content'])} chars)", evidence=[ev])

    reg.register(Tool("files.list", "List files in the company shared folder (or 'out/' for files you produced).", obj({"path": S}), Risk.R0_READ, files_list))
    reg.register(Tool("files.read", "Read a file from the company shared folder (csv/xlsx return rows as JSON). Use 'out/<name>' to read a file you wrote.", obj({"path": S}, ["path"]), Risk.R0_READ, files_read))
    reg.register(Tool("files.summarize_table", "Exact column sums / counts for a csv or xlsx file (use this instead of adding numbers yourself).",
                      obj({"path": S, "sum_columns": {"type": "array", "items": S}, "group_by": S}, ["path"]), Risk.R0_READ, files_summarize))
    reg.register(Tool("files.write", "Write a text output file (e.g. a report). Saved as task evidence.", obj({"path": S, "content": S}, ["path", "content"]), Risk.R1_REVERSIBLE, files_write))

    # ---------------------------------------------------------------- read-only HTTP to company systems
    def http_get(ctx, a):
        a = {**a, "url": resolve_company_url(a["url"], pack.systems)}
        u = urlparse(a["url"])
        if u.netloc not in allowed_hosts:
            return ToolResult(False, error=f"host {u.netloc or a['url']} is not a registered company system", error_kind=ErrorKind.PERMANENT)
        try:
            r = httpx.get(a["url"], timeout=10, headers={"accept": "application/json, text/html"})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            return ToolResult(False, error=f"{type(e).__name__}: {e}", error_kind=ErrorKind.TRANSIENT)
        if r.status_code >= 500:
            return ToolResult(False, error=f"HTTP {r.status_code}", error_kind=ErrorKind.TRANSIENT)
        if r.status_code >= 400:
            return ToolResult(False, error=f"HTTP {r.status_code}: {r.text[:200]}", error_kind=ErrorKind.PERMANENT)
        return ToolResult(True, r.text[:12000])

    reg.register(Tool("http.get", "Read-only GET of a URL on a registered company system (API endpoints return JSON).", obj({"url": S}, ["url"]), Risk.R0_READ, http_get))

    # ---------------------------------------------------------------- outbox (mock mail server)
    def outbox_send(ctx, a):
        mail = {"id": f"mail_{abs(hash(canonical(a) + ctx.task_id)) % 10**8:08d}", "task_id": ctx.task_id, **{k: a[k] for k in ("to", "subject", "body")}}
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with outbox_path.open("a") as f:
            f.write(json.dumps(mail) + "\n")
        ev = ctx.evidence.save(ctx.task_id, f"email_to_{a['to']}.txt", f"To: {a['to']}\nSubject: {a['subject']}\n\n{a['body']}", kind="email", actor="agent")
        return ToolResult(True, f"email {mail['id']} sent to {a['to']}", evidence=[ev])

    def outbox_list(ctx, a):
        if not outbox_path.exists():
            return ToolResult(True, "[]")
        mails = [m for m in map(json.loads, outbox_path.read_text().splitlines()) if m["task_id"] == ctx.task_id]
        return ToolResult(True, json.dumps(mails))

    reg.register(Tool("outbox.send", "Send an email via the company mail system. Internal addresses are allowed; external ones need approval.",
                      obj({"to": S, "subject": S, "body": S}, ["to", "subject", "body"]), Risk.R2_EXTERNAL, outbox_send, system="mail", mutating=True))
    reg.register(Tool("outbox.list", "List emails sent during this task (to confirm they went out).", obj({}), Risk.R0_READ, outbox_list))

    # ---------------------------------------------------------------- directory
    def directory_lookup(ctx, a):
        hits = pack.lookup_person(a["query"])
        return ToolResult(True, json.dumps(hits) if hits else f"nobody matches '{a['query']}'")

    reg.register(Tool("directory.lookup", "Find people (email, role) in the company directory by name, email or role.", obj({"query": S}, ["query"]), Risk.R0_READ, directory_lookup))
