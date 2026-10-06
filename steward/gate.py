"""Policy gate: the single choke point every tool call passes through.

policy decision -> (if approval needed) scoped single-use grant -> audit event.
Grants are bound to (task, tool, exact args), expire, and are consumed on use, so
approving "refund 2500 to order 77" cannot be replayed for "refund 9000 to order 77".
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .audit import AuditLog, canonical
from .policy import PolicyEngine, Verdict
from .store import Store, new_id, now
from .tools.base import Risk, Tool


def fingerprint(task_id: str, tool: str, args: dict) -> str:
    return hashlib.sha256(f"{task_id}|{tool}|{canonical(args)}".encode()).hexdigest()[:32]


@dataclass
class Decision:
    verdict: Verdict
    risk: Risk
    reasons: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    approval_id: str | None = None
    approver: str | None = None
    via_grant: bool = False
    human_comment: str | None = None     # set when a human rejected this exact action

    def to_model_text(self) -> str:
        if self.verdict == Verdict.DENY:
            msg = "DENIED by policy: " + "; ".join(self.reasons)
            if self.human_comment:
                msg += f" | reviewer said: {self.human_comment}"
            return msg + ". Do not retry this action; choose a permitted alternative or ask the user."
        return ("PENDING APPROVAL (" + "; ".join(self.reasons) + f") approval_id={self.approval_id}"
                + (f", approver={self.approver}" if self.approver else "")
                + ". This run will pause; do not try to work around it.")


class Gate:
    def __init__(self, store: Store, audit: AuditLog, engine: PolicyEngine, grant_ttl_s: int = 24 * 3600):
        self.store, self.audit, self.engine, self.grant_ttl_s = store, audit, engine, grant_ttl_s

    def check(self, task_id: str, tool: Tool, args: dict, *, role: str, task: dict | None = None,
              system: str | None = None, actor: str = "agent", risk_override: Risk | None = None) -> Decision:
        risk = risk_override if risk_override is not None else tool.risk_for(args)
        pd = self.engine.evaluate(tool=tool.name, args=args, risk=risk, role=role,
                                  system=system or tool.system, task=task)
        dec = Decision(pd.verdict, risk, pd.reasons, pd.rule_ids, approver=pd.approver)
        fp = fingerprint(task_id, tool.name, args)

        if pd.verdict == Verdict.REQUIRE_APPROVAL:
            dec = self._resolve_approval(task_id, tool.name, args, fp, dec)

        self.audit.append(task_id, "policy.decision", actor, {
            "tool": tool.name, "args": args, "risk": risk.name, "verdict": dec.verdict.value,
            "reasons": dec.reasons, "rules": dec.rule_ids, "approval_id": dec.approval_id,
            "via_grant": dec.via_grant,
        })
        return dec

    # ------------------------------------------------------------ approvals
    def _resolve_approval(self, task_id: str, tool_name: str, args: dict, fp: str, dec: Decision) -> Decision:
        t = now()
        with self.store.tx() as c:
            # 1. a usable grant? consume atomically.
            row = c.execute(
                "SELECT id FROM approvals WHERE task_id=? AND fingerprint=? AND status='approved' "
                "AND (expires_at IS NULL OR expires_at > ?) ORDER BY decided_at DESC LIMIT 1",
                (task_id, fp, t)).fetchone()
            if row:
                c.execute("UPDATE approvals SET status='consumed' WHERE id=?", (row["id"],))
                dec.verdict, dec.via_grant, dec.approval_id = Verdict.ALLOW, True, row["id"]
                dec.reasons = dec.reasons + [f"approved via grant {row['id']}"]
                return dec
            # 2. this exact action was rejected by a human -> hard deny (with their comment)
            rej = c.execute(
                "SELECT id, comment, decided_by FROM approvals WHERE task_id=? AND fingerprint=? AND status='rejected' "
                "ORDER BY decided_at DESC LIMIT 1", (task_id, fp)).fetchone()
            if rej:
                dec.verdict, dec.approval_id = Verdict.DENY, rej["id"]
                dec.human_comment = rej["comment"]
                dec.reasons = dec.reasons + [f"rejected by {rej['decided_by']}"]
                return dec
            # 3. already pending? reuse. otherwise open a new request.
            pend = c.execute(
                "SELECT id FROM approvals WHERE task_id=? AND fingerprint=? AND status='pending'",
                (task_id, fp)).fetchone()
            if pend:
                dec.approval_id = pend["id"]
                return dec
            aid = new_id("apr")
            c.execute(
                "INSERT INTO approvals(id, task_id, fingerprint, tool, args, risk, reasons, approver, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
                (aid, task_id, fp, tool_name, canonical(args), int(dec.risk), json.dumps(dec.reasons), dec.approver, t))
            dec.approval_id = aid
        self.audit.append(task_id, "approval.requested", "gate",
                          {"approval_id": aid, "tool": tool_name, "args": args, "risk": dec.risk.name,
                           "reasons": dec.reasons, "approver": dec.approver})
        return dec

    def decide(self, approval_id: str, *, approve: bool, by: str, comment: str | None = None) -> dict:
        t = now()
        with self.store.tx() as c:
            row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise KeyError(approval_id)
            if row["status"] != "pending":
                raise ValueError(f"approval {approval_id} is already {row['status']}")
            status = "approved" if approve else "rejected"
            c.execute("UPDATE approvals SET status=?, decided_by=?, comment=?, decided_at=?, expires_at=? WHERE id=?",
                      (status, by, comment, t, t + self.grant_ttl_s if approve else None, approval_id))
        self.audit.append(row["task_id"], f"approval.{status}", by,
                          {"approval_id": approval_id, "tool": row["tool"], "comment": comment})
        return {"approval_id": approval_id, "task_id": row["task_id"], "status": status}

    def pending(self, task_id: str | None = None) -> list[dict]:
        sql, p = "SELECT * FROM approvals WHERE status='pending'", ()
        if task_id:
            sql, p = sql + " AND task_id=?", (task_id,)
        out = []
        for r in self.store.query(sql + " ORDER BY created_at", p):
            d = dict(r)
            d["args"], d["reasons"] = json.loads(d["args"]), json.loads(d["reasons"])
            out.append(d)
        return out
