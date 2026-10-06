"""Tool abstraction: every capability the agent has is a Tool with a declared risk.

Adding a new capability for a new company workflow = registering a Tool (or just
using the generic browser/http/files tools and describing the system in the
Company Pack). The runtime never special-cases a tool by name.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Union


class Risk(IntEnum):
    R0_READ = 0          # observe only: read page, read file, query
    R1_REVERSIBLE = 1    # undoable / internal write: draft, note, create ticket comment
    R2_EXTERNAL = 2      # visible outside or hard to undo: send email, update customer record
    R3_CRITICAL = 3      # financial / destructive / permission changes: refund, delete, grant admin


class ErrorKind:
    TRANSIENT = "transient"        # timeout, 5xx, flaky UI -> retry with backoff
    PERMANENT = "permanent"        # bad args, 4xx, element missing -> do not blindly retry; agent must adapt
    NEEDS_HUMAN = "needs_human"    # captcha, ambiguity, missing creds -> escalate


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: str | None = None
    error_kind: str | None = None
    evidence: list[dict] = field(default_factory=list)   # evidence records saved by the tool
    signal: str | None = None        # control signal for the runtime, e.g. "pause_for_approval"
    meta: dict = field(default_factory=dict)             # structured extras (approval ids, ...)

    def to_model_text(self, limit: int = 6000) -> str:
        import json
        body = self.output if self.ok else {"error": self.error, "error_kind": self.error_kind}
        text = body if isinstance(body, str) else json.dumps(body, default=str, ensure_ascii=False)
        return text if len(text) <= limit else text[:limit] + f"... [truncated {len(text) - limit} chars]"


@dataclass
class ToolContext:
    """Everything a tool handler may touch. Passed explicitly; no globals."""
    task_id: str
    store: Any
    audit: Any
    evidence: Any
    workdir: str
    env: dict = field(default_factory=dict)       # runtime handles (browser session, http clients, pack)


RiskSpec = Union[Risk, Callable[[dict], Risk]]


@dataclass
class Tool:
    name: str                       # e.g. "browser.click", "files.read", "billing.refund"
    description: str
    input_schema: dict
    risk: RiskSpec                  # static or computed from args (e.g. http method)
    handler: Callable[[ToolContext, dict], ToolResult]
    system: str | None = None       # which company system it touches (matches systems.yaml)
    mutating: bool = False          # needs an idempotency key / is never auto-retried blindly

    def risk_for(self, args: dict) -> Risk:
        return self.risk(args) if callable(self.risk) else self.risk

    def to_anthropic(self) -> dict:
        # Anthropic tool names allow [a-zA-Z0-9_-]; keep our dotted names internally.
        return {"name": self.name.replace(".", "__"), "description": self.description,
                "input_schema": self.input_schema}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name.replace("__", "."))

    def names(self) -> list[str]:
        return sorted(self._tools)

    def select(self, patterns: list[str] | None = None) -> list[Tool]:
        """Least-privilege view: only tools matching any glob in `patterns`."""
        if patterns is None:
            return list(self._tools.values())
        return [t for n, t in sorted(self._tools.items()) if any(fnmatch.fnmatch(n, p) for p in patterns)]


def resolve_company_url(url: str, systems: dict) -> str:
    """Turn '/billing/orders/A-1001' into the full URL on the registered system whose prefix matches.
    Absolute URLs and unknown paths are returned unchanged (the host allow-list still decides)."""
    from urllib.parse import urlparse
    if not url.startswith("/"):
        return url
    for sysd in systems.values():
        pre, base = sysd.get("prefix"), sysd.get("base_url")
        if pre and base and (url == pre or url.startswith(pre.rstrip("/") + "/")):
            b = urlparse(base)
            return f"{b.scheme}://{b.netloc}{url}"
    return url
