"""Deterministic policy engine. The LLM can propose; only this module decides.

Policies live in the Company Pack (policies.yaml). A rule's `when` is a small,
sandboxed expression over: tool, args, risk, system, role, task.
Fail-closed: if a deny/require_approval rule's expression errors, it counts as
matched; if an allow rule errors, it counts as not matched.
"""
from __future__ import annotations

import ast
import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import yaml

from .tools.base import Risk


class Verdict(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


# ---------------------------------------------------------------- safe expressions
class AttrDict(dict):
    """dict with attribute access; missing keys -> None; nested dicts wrapped."""
    def __getattr__(self, k: str) -> Any:
        if k.startswith("_"):
            raise AttributeError(k)
        return _wrap(self.get(k))


def _wrap(v: Any) -> Any:
    if isinstance(v, dict) and not isinstance(v, AttrDict):
        return AttrDict(v)
    return v


_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
    ast.Name, ast.Load, ast.Attribute, ast.Subscript, ast.Constant, ast.List, ast.Tuple,
    ast.Set, ast.Call, ast.IfExp,
)
_SAFE_FUNCS = {"len": len, "int": int, "float": float, "str": str, "abs": abs, "min": min, "max": max}
_SAFE_METHODS = {"startswith", "endswith", "lower", "upper", "strip", "get"}


def _validate(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private attribute access is not allowed")
        if isinstance(node, ast.Call):
            f = node.func
            ok = (isinstance(f, ast.Name) and f.id in _SAFE_FUNCS) or \
                 (isinstance(f, ast.Attribute) and f.attr in _SAFE_METHODS)
            if not ok:
                raise ValueError("disallowed function call")


def safe_eval(expr: str, env: dict[str, Any]) -> Any:
    tree = ast.parse(expr, mode="eval")
    _validate(tree)
    scope = {k: _wrap(v) for k, v in env.items()}
    return eval(compile(tree, "<policy>", "eval"), {"__builtins__": {}, **_SAFE_FUNCS}, scope)  # noqa: S307


# ---------------------------------------------------------------- engine
@dataclass
class PolicyDecision:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    approver: str | None = None


class PolicyEngine:
    def __init__(self, policy: dict):
        self.policy = policy or {}
        d = self.policy.get("defaults", {})
        self.auto_allow_max_risk = Risk(d.get("auto_allow_max_risk", 1))
        self.unknown_role = d.get("unknown_role", "deny")
        self.roles: dict[str, dict] = self.policy.get("roles", {})
        self.rules: list[dict] = self.policy.get("rules", [])

    @classmethod
    def from_file(cls, path: str) -> "PolicyEngine":
        with open(path) as f:
            return cls(yaml.safe_load(f))

    @staticmethod
    def _match(name: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in patterns)

    def tool_visible(self, role: str, tool_name: str) -> bool:
        """Used to build the toolset shown to the model (least privilege)."""
        r = self.roles.get(role)
        if r is None:
            return False
        if self._match(tool_name, r.get("deny_tools", [])):
            return False
        return self._match(tool_name, r.get("allow_tools", []))

    def evaluate(self, *, tool: str, args: dict, risk: Risk, role: str,
                 system: str | None = None, task: dict | None = None) -> PolicyDecision:
        # 1) role gate
        r = self.roles.get(role)
        if r is None:
            return PolicyDecision(Verdict.DENY, [f"unknown role '{role}'"], ["role.unknown"])
        if self._match(tool, r.get("deny_tools", [])):
            return PolicyDecision(Verdict.DENY, [f"role '{role}' is explicitly denied '{tool}'"], ["role.deny"])
        if not self._match(tool, r.get("allow_tools", [])):
            return PolicyDecision(Verdict.DENY, [f"role '{role}' is not permitted to use '{tool}'"], ["role.not_allowed"])

        # 2) company rules
        env = {"tool": tool, "args": args, "risk": int(risk), "system": system, "role": role, "task": task or {}}
        deny, approve, allow = [], [], []
        for rule in self.rules:
            action = rule.get("action", "require_approval")
            try:
                matched = bool(safe_eval(rule["when"], env))
            except Exception:                      # fail closed
                matched = action != "allow"
            if matched:
                {"deny": deny, "require_approval": approve, "allow": allow}[action].append(rule)

        if deny:
            return PolicyDecision(Verdict.DENY, [x.get("reason", x["id"]) for x in deny], [x["id"] for x in deny])
        if approve:
            return PolicyDecision(Verdict.REQUIRE_APPROVAL, [x.get("reason", x["id"]) for x in approve],
                                  [x["id"] for x in approve],
                                  approver=next((x["approver"] for x in approve if x.get("approver")), None))
        if allow:
            return PolicyDecision(Verdict.ALLOW, [x.get("reason", x["id"]) for x in allow], [x["id"] for x in allow])

        # 3) default by risk level
        if risk <= self.auto_allow_max_risk:
            return PolicyDecision(Verdict.ALLOW, [f"risk {risk.name} within auto-allow ({self.auto_allow_max_risk.name})"],
                                  ["default.risk"])
        return PolicyDecision(Verdict.REQUIRE_APPROVAL,
                              [f"risk {risk.name} exceeds auto-allow ({self.auto_allow_max_risk.name})"],
                              ["default.risk"], approver=self.policy.get("defaults", {}).get("default_approver"))
