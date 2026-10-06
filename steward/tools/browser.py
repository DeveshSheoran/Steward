"""Browser tools (Playwright) with a network-level policy gate.

Safety does NOT depend on the model behaving: every non-GET request the page makes is intercepted,
classified against systems.yaml, and sent through the Gate *before* it leaves the browser.
Allowed writes are sent with a deterministic Idempotency-Key and retried on 5xx/timeouts with
backoff, so a retry after an ambiguous failure cannot apply the action twice.
"""
from __future__ import annotations

import fnmatch
import json
import random
import time
from urllib.parse import parse_qs, urlparse

from ..audit import AuditLog, EvidenceStore
from ..gate import Gate, Decision, fingerprint
from ..policy import Verdict
from .base import resolve_company_url, ErrorKind, Risk, Tool, ToolContext, ToolRegistry, ToolResult

NET_TOOL = Tool("browser.net_mutation", "network write issued by the page", {"type": "object"},
                Risk.R2_EXTERNAL, lambda c, a: ToolResult(True), mutating=True)

SNAPSHOT_JS = """
() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-sref]').forEach(e => e.removeAttribute('data-sref'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button]';
  const out = []; let n = 0;
  document.querySelectorAll(sel).forEach(e => {
    if (!vis(e)) return;
    const ref = 'e' + (++n); e.setAttribute('data-sref', ref);
    let label = e.getAttribute('aria-label') || '';
    if (!label && e.id) { const l = document.querySelector('label[for="' + e.id + '"]'); if (l) label = l.innerText; }
    if (!label) { const l = e.closest('label'); if (l) label = l.innerText; }
    if (!label) label = e.getAttribute('placeholder') || '';
    const d = { ref, tag: e.tagName.toLowerCase(), type: e.getAttribute('type') || '', name: e.getAttribute('name') || '',
                label: label.trim(), text: (e.innerText || '').trim().slice(0, 80), value: (e.value || '').toString().slice(0, 60) };
    if (e.tagName === 'A') d.href = e.getAttribute('href');
    if (e.tagName === 'SELECT') d.options = Array.from(e.options).map(o => o.text);
    out.push(d);
  });
  return { url: location.href, title: document.title, text: (document.body.innerText || '').slice(0, 2500), elements: out };
}
"""


def _coerce(v: str):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


class BrowserSession:
    def __init__(self, *, cfg, gate: Gate, audit: AuditLog, evidence: EvidenceStore, systems: dict,
                 task_id: str, role: str, task_ctx: dict | None = None, max_net_retries: int = 3):
        self.cfg, self.gate, self.audit, self.evidence = cfg, gate, audit, evidence
        self.systems, self.task_id, self.role, self.task_ctx = systems, task_id, role, task_ctx or {}
        self.max_net_retries = max_net_retries
        self.allowed_hosts = {urlparse(s["base_url"]).netloc for s in systems.values()}
        self._pw = self._browser = self._ctx = self.page = None
        self.blocked: list[Decision] = []      # gate decisions that stopped a request during the current action
        self.applied: list[dict] = []          # writes that went through during the current action
        self.failed: list[dict] = []           # writes that failed even after retries

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.page is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        kw = {"headless": self.cfg.headless}
        if self.cfg.browser_channel:
            kw["channel"] = self.cfg.browser_channel
        if self.cfg.browser_path:
            kw["executable_path"] = self.cfg.browser_path
        self._browser = self._pw.chromium.launch(**kw)
        self._ctx = self._browser.new_context(viewport={"width": 1100, "height": 800})
        self._ctx.set_default_timeout(8000)
        self.page = self._ctx.new_page()
        self.page.route("**/*", self._route)

    def close(self) -> None:
        for obj, m in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                getattr(obj, m)()
            except Exception:
                pass
        self.page = None

    # ------------------------------------------------------------ network gate
    def _classify(self, method: str, path: str) -> tuple[str | None, dict | None]:
        system = next((n for n, s in self.systems.items() if path.startswith(s.get("prefix", "\0"))), None)
        if system:
            for m in self.systems[system].get("mutations", []):
                if fnmatch.fnmatch(f"{method} {path}", m["match"]):
                    return system, m
        return system, None

    def _route(self, route, request) -> None:
        u = urlparse(request.url)
        if u.scheme in ("data", "about", "blob"):
            return route.continue_()
        if u.netloc not in self.allowed_hosts:
            self.audit.append(self.task_id, "net.blocked", "browser", {"url": request.url, "reason": "host not in systems.yaml"})
            return route.abort("blockedbyclient")
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return route.continue_()

        system, rule = self._classify(request.method, u.path)
        form = {k: _coerce(v[0]) for k, v in parse_qs(request.post_data or "", keep_blank_values=True).items()}
        args = {"method": request.method, "path": u.path, **form}
        risk = _risk(rule["risk"] if rule else "R2")
        dec = self.gate.check(self.task_id, NET_TOOL, args, role=self.role, task=self.task_ctx, system=system,
                              actor="browser", risk_override=risk)
        if dec.verdict != Verdict.ALLOW:
            self.blocked.append(dec)
            return route.abort("blockedbyclient")

        key = fingerprint(self.task_id, NET_TOOL.name, args)
        headers = {**request.headers, "idempotency-key": key}
        label = rule.get("label") if rule else "unclassified write"
        for attempt in range(1, self.max_net_retries + 1):
            try:
                resp = route.fetch(headers=headers, max_redirects=0, timeout=15000)
                if resp.status < 500:
                    rec = {"label": label, "system": system, "path": u.path, "status": resp.status,
                           "attempts": attempt, "idempotency_key": key, "args": args}
                    self.applied.append(rec)
                    self.audit.append(self.task_id, "net.write", "browser", rec)
                    return route.fulfill(response=resp)
                err = f"HTTP {resp.status}"
            except Exception as e:                       # timeout / connection reset
                err = f"{type(e).__name__}: {e}"[:200]
            self.audit.append(self.task_id, "net.retry", "browser", {"path": u.path, "attempt": attempt, "error": err})
            if attempt < self.max_net_retries:
                time.sleep(min(2.0, 0.3 * 2 ** (attempt - 1)) * (0.7 + 0.6 * random.random()))
        self.failed.append({"label": label, "path": u.path, "error": err, "attempts": self.max_net_retries})
        return route.abort("failed")

    # ------------------------------------------------------------ actions
    def _begin(self) -> None:
        self.blocked, self.applied, self.failed = [], [], []

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=8000)
        except Exception:
            pass

    def _nav_safe(self, fn, tries: int = 4):
        """Run a page operation; if a navigation destroys the execution context mid-call
        (redirect after a form POST, aborted submit -> error page), wait for load and retry."""
        last = None
        for _ in range(tries):
            try:
                return fn()
            except Exception as e:
                msg = str(e)
                if "Execution context was destroyed" not in msg and "navigation" not in msg.lower():
                    raise
                last = e
                try:
                    self.page.wait_for_load_state("load", timeout=5000)
                except Exception:
                    pass
                self.page.wait_for_timeout(100)
        raise last

    def snapshot(self) -> dict:
        return self._nav_safe(lambda: self.page.evaluate(SNAPSHOT_JS))

    def _compact(self, snap: dict) -> str:
        lines = [f"URL: {snap['url']}", f"TITLE: {snap['title']}", "ELEMENTS:"]
        for e in snap["elements"]:
            bits = [f"[{e['ref']}]", e["tag"] + (f":{e['type']}" if e["type"] else "")]
            if e["name"]: bits.append(f"name={e['name']}")
            lab = e["label"] or e["text"]
            if lab: bits.append(f'"{lab}"')
            if e["value"]: bits.append(f"value={e['value']!r}")
            if e.get("href"): bits.append(f"-> {e['href']}")
            if e.get("options"): bits.append("options=" + "|".join(e["options"]))
            lines.append(" ".join(bits))
        lines += ["PAGE TEXT:", snap["text"]]
        return "\n".join(lines)

    def _outcome(self, ctx: ToolContext, what: str) -> ToolResult:
        """Common post-action handling: blocked / failed / applied writes, evidence, fresh snapshot."""
        self._settle()
        if self.blocked:
            d = self.blocked[0]
            if d.verdict == Verdict.REQUIRE_APPROVAL:
                return ToolResult(False, error=d.to_model_text(), error_kind=ErrorKind.NEEDS_HUMAN,
                                  signal="pause_for_approval", meta={"approval_id": d.approval_id, "approver": d.approver})
            return ToolResult(False, error=d.to_model_text(), error_kind=ErrorKind.PERMANENT)
        if self.failed:
            f = self.failed[0]
            return ToolResult(False, error=f"The write '{f['label']}' failed after {f['attempts']} automatic retries ({f['error']}). "
                                           "It was NOT confirmed as applied; check the system before retrying.",
                              error_kind=ErrorKind.TRANSIENT)
        ev = []
        if self.applied:
            ev.append(self.evidence.save(self.task_id, f"after_{what}.png", self.page.screenshot(), kind="screenshot",
                                         meta={"applied": [a["label"] for a in self.applied]}, actor="browser"))
        snap = self.snapshot()
        out = self._compact(snap)
        if self.applied:
            out = "WRITES APPLIED: " + "; ".join(f"{a['label']} (HTTP {a['status']}, attempts={a['attempts']})" for a in self.applied) + "\n" + out
        return ToolResult(True, out, evidence=ev)

    def goto(self, ctx: ToolContext, url: str) -> ToolResult:
        url = resolve_company_url(url, self.systems)
        self._begin()
        try:
            self.page.goto(url, wait_until="load")
        except Exception as e:
            kind = ErrorKind.PERMANENT if "ERR_BLOCKED_BY_CLIENT" in str(e) else ErrorKind.TRANSIENT
            return ToolResult(False, error=f"navigation failed: {str(e)[:200]}", error_kind=kind)
        return self._outcome(ctx, "goto")

    def click(self, ctx: ToolContext, ref: str) -> ToolResult:
        self._begin()
        loc = self.page.locator(f'[data-sref="{ref}"]')
        if self._nav_safe(loc.count) == 0:
            return ToolResult(False, error=f"unknown ref {ref}; take a fresh browser.snapshot", error_kind=ErrorKind.PERMANENT)
        try:
            loc.first.click()
        except Exception as e:
            return ToolResult(False, error=f"click failed: {str(e)[:200]}", error_kind=ErrorKind.TRANSIENT)
        return self._outcome(ctx, "click")

    def fill(self, ctx: ToolContext, ref: str, value: str) -> ToolResult:
        loc = self.page.locator(f'[data-sref="{ref}"]')
        if self._nav_safe(loc.count) == 0:
            return ToolResult(False, error=f"unknown ref {ref}; take a fresh browser.snapshot", error_kind=ErrorKind.PERMANENT)
        loc.first.fill(str(value))
        return ToolResult(True, f"filled {ref}")

    def select(self, ctx: ToolContext, ref: str, value: str) -> ToolResult:
        loc = self.page.locator(f'[data-sref="{ref}"]')
        if self._nav_safe(loc.count) == 0:
            return ToolResult(False, error=f"unknown ref {ref}; take a fresh browser.snapshot", error_kind=ErrorKind.PERMANENT)
        try:
            loc.first.select_option(label=str(value))
        except Exception:
            loc.first.select_option(value=str(value))
        return ToolResult(True, f"selected {value} in {ref}")


def _risk(name: str) -> Risk:
    return {"R0": Risk.R0_READ, "R1": Risk.R1_REVERSIBLE, "R2": Risk.R2_EXTERNAL, "R3": Risk.R3_CRITICAL}[name]


# ---------------------------------------------------------------- registry glue
def _sess(ctx: ToolContext) -> BrowserSession:
    b: BrowserSession = ctx.env["browser"]
    b.start()
    return b


def register_browser_tools(reg: ToolRegistry) -> None:
    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    S = {"type": "string"}
    reg.register(Tool("browser.goto", "Open a URL in the browser (only company systems are reachable). Returns the page's interactive elements with refs.",
                      obj({"url": S}, ["url"]), Risk.R0_READ, lambda c, a: _sess(c).goto(c, a["url"])))
    reg.register(Tool("browser.snapshot", "Get the current page: URL, visible text and interactive elements with refs like e3.",
                      obj({}, []), Risk.R0_READ, lambda c, a: ToolResult(True, _sess(c)._compact(_sess(c).snapshot()))))
    reg.register(Tool("browser.click", "Click an element by its ref from the latest snapshot. Submitting a form may be held for human approval by policy.",
                      obj({"ref": S}, ["ref"]), Risk.R1_REVERSIBLE, lambda c, a: _sess(c).click(c, a["ref"])))
    reg.register(Tool("browser.fill", "Type text into an input/textarea by ref.",
                      obj({"ref": S, "value": S}, ["ref", "value"]), Risk.R1_REVERSIBLE, lambda c, a: _sess(c).fill(c, a["ref"], a["value"])))
    reg.register(Tool("browser.select", "Choose an option in a <select> by ref (option text or value).",
                      obj({"ref": S, "value": S}, ["ref", "value"]), Risk.R1_REVERSIBLE, lambda c, a: _sess(c).select(c, a["ref"], a["value"])))
    reg.register(Tool("browser.screenshot", "Save a screenshot of the current page as evidence.",
                      obj({"note": S}, []), Risk.R0_READ,
                      lambda c, a: ToolResult(True, "screenshot saved", evidence=[c.evidence.save(
                          c.task_id, "screenshot.png", _sess(c).page.screenshot(), kind="screenshot", meta={"note": a.get("note", "")}, actor="agent")])))
