import pytest
import yaml

from steward.audit import AuditLog, EvidenceStore
from steward.gate import Gate
from steward.policy import PolicyEngine, Verdict, safe_eval
from steward.store import Store
from steward.tools.base import Risk, Tool, ToolResult

POLICY_PATH = "company_pack/policies.yaml"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "s.db")
    audit = AuditLog(store)
    engine = PolicyEngine.from_file(POLICY_PATH)
    return store, audit, engine, Gate(store, audit, engine), EvidenceStore(store, audit, tmp_path / "ev")


def tool(name, risk, system=None):
    return Tool(name, "d", {"type": "object"}, risk, lambda c, a: ToolResult(True), system=system)


# ------------------------------------------------------------------ audit
def test_audit_chain_ok_and_detects_modification(env):
    store, audit, *_ = env
    for i in range(5):
        audit.append("t1", "step", "agent", {"i": i})
    assert audit.verify().ok and audit.verify().checked == 5
    store.execute("UPDATE events SET payload='{\"i\":99}' WHERE seq=3")
    v = audit.verify()
    assert not v.ok and v.broken_at_seq == 3


def test_audit_detects_deleted_row(env):
    store, audit, *_ = env
    for i in range(5):
        audit.append("t1", "step", "agent", {"i": i})
    store.execute("DELETE FROM events WHERE seq=2")
    v = audit.verify()
    assert not v.ok and v.broken_at_seq == 3


def test_evidence_hash_and_tamper(env, tmp_path):
    store, audit, _, _, ev = env
    rec = ev.save("t1", "page.html", "<h1>hi</h1>", kind="dom")
    assert ev.verify("t1") == []
    open(rec["path"], "w").write("tampered")
    assert ev.verify("t1")[0]["problem"] == "hash mismatch"
    assert any(e["kind"] == "evidence.added" for e in audit.events("t1"))


# ------------------------------------------------------------------ safe_eval
@pytest.mark.parametrize("expr", ["__import__('os')", "args.__class__", "open('x')", "[x for x in args]", "(lambda: 1)()"])
def test_safe_eval_blocks_escape(expr):
    with pytest.raises(Exception):
        safe_eval(expr, {"args": {"a": 1}})


def test_safe_eval_basic():
    assert safe_eval("args.amount > 10 and args.x.lower() == 'a'", {"args": {"amount": 11, "x": "A"}})


# ------------------------------------------------------------------ policy
def ev(engine, name, args, risk, role="ops-agent", system=None):
    return engine.evaluate(tool=name, args=args, risk=risk, role=role, system=system)


def test_default_risk_and_roles(env):
    _, _, engine, *_ = env
    assert ev(engine, "browser.snapshot", {}, Risk.R0_READ).verdict == Verdict.ALLOW
    assert ev(engine, "browser.click", {}, Risk.R1_REVERSIBLE).verdict == Verdict.ALLOW
    assert ev(engine, "browser.submit", {}, Risk.R3_CRITICAL).verdict == Verdict.REQUIRE_APPROVAL
    assert ev(engine, "shell.run", {}, Risk.R0_READ).verdict == Verdict.DENY          # role deny
    assert ev(engine, "anything", {}, Risk.R0_READ, role="intern").verdict == Verdict.DENY  # unknown role


def test_refund_thresholds(env):
    _, _, engine, *_ = env
    assert ev(engine, "browser.net_mutation", {"amount": 1500}, Risk.R1_REVERSIBLE, system="billing").verdict == Verdict.ALLOW
    d = ev(engine, "browser.net_mutation", {"amount": 2500}, Risk.R1_REVERSIBLE, system="billing")
    assert d.verdict == Verdict.REQUIRE_APPROVAL and d.approver == "finance-manager"
    assert ev(engine, "browser.net_mutation", {"amount": 80000}, Risk.R1_REVERSIBLE, system="billing").verdict == Verdict.DENY


def test_deny_beats_approval_and_allow_rule_overrides_default(env):
    _, _, engine, *_ = env
    # 80000 matches both refund-large (approval) and refund-hard-cap (deny) -> deny wins
    assert ev(engine, "x.y", {"amount": 80000}, Risk.R0_READ, system="billing").verdict == Verdict.DENY
    # internal mail is R2 (would need approval by default) but an allow rule exempts it
    assert ev(engine, "outbox.send", {"to": "a@northwind.example"}, Risk.R2_EXTERNAL).verdict == Verdict.ALLOW
    assert ev(engine, "outbox.send", {"to": "a@gmail.com"}, Risk.R2_EXTERNAL).verdict == Verdict.REQUIRE_APPROVAL


def test_fail_closed_on_broken_rule():
    eng = PolicyEngine({"roles": {"r": {"allow_tools": ["*"]}},
                        "rules": [{"id": "bad", "when": "args.amount > 5", "action": "require_approval"}]})
    # args.amount missing -> None > 5 raises TypeError -> rule treated as matched (fail closed)
    assert eng.evaluate(tool="t", args={}, risk=Risk.R0_READ, role="r").verdict == Verdict.REQUIRE_APPROVAL


def test_admin_group_needs_security_approval(env):
    _, _, engine, *_ = env
    d = ev(engine, "browser.net_mutation", {"group": "Admins"}, Risk.R1_REVERSIBLE, system="admin_console")
    assert d.verdict == Verdict.REQUIRE_APPROVAL and d.approver == "it-security"
    assert ev(engine, "browser.net_mutation", {"group": "staff"}, Risk.R1_REVERSIBLE, system="admin_console").verdict == Verdict.ALLOW


# ------------------------------------------------------------------ gate / grants
def test_gate_approval_lifecycle_single_use_and_exact_args(env):
    store, audit, engine, gate, _ = env
    t = tool("http.request", Risk.R3_CRITICAL, "billing")
    a = {"order": 77, "amount": 2500}

    d1 = gate.check("t1", t, a, role="ops-agent")
    assert d1.verdict == Verdict.REQUIRE_APPROVAL and d1.approval_id
    d1b = gate.check("t1", t, a, role="ops-agent")                 # retry does not spawn a second request
    assert d1b.approval_id == d1.approval_id and len(gate.pending("t1")) == 1

    gate.decide(d1.approval_id, approve=True, by="finance-manager", comment="ok")
    # grant is bound to exact args: a different amount still needs its own approval
    assert gate.check("t1", t, {"order": 77, "amount": 9000}, role="ops-agent").verdict == Verdict.REQUIRE_APPROVAL
    d2 = gate.check("t1", t, a, role="ops-agent")
    assert d2.verdict == Verdict.ALLOW and d2.via_grant
    # single use: second execution needs a new approval
    assert gate.check("t1", t, a, role="ops-agent").verdict == Verdict.REQUIRE_APPROVAL
    # grant is bound to the task
    gate.decide(gate.pending("t1")[0]["id"], approve=True, by="m")
    assert gate.check("t2", t, a, role="ops-agent").verdict == Verdict.REQUIRE_APPROVAL
    assert audit.verify().ok


def test_gate_rejection_becomes_deny_with_comment(env):
    _, _, _, gate, _ = env
    t = tool("http.request", Risk.R3_CRITICAL, "billing")
    a = {"order": 5, "amount": 3000}
    d = gate.check("t1", t, a, role="ops-agent")
    gate.decide(d.approval_id, approve=False, by="finance-manager", comment="enterprise accounts go via CSM")
    d2 = gate.check("t1", t, a, role="ops-agent")
    assert d2.verdict == Verdict.DENY and "enterprise accounts" in d2.to_model_text()
    with pytest.raises(ValueError):
        gate.decide(d.approval_id, approve=True, by="x")           # cannot flip a decided approval


def test_relative_company_paths_resolve_to_registered_system():
    from steward.tools.base import resolve_company_url
    systems = {"billing": {"prefix": "/billing", "base_url": "http://127.0.0.1:8800/billing/orders"}}
    assert resolve_company_url("/billing/api/orders", systems) == "http://127.0.0.1:8800/billing/api/orders"
    assert resolve_company_url("/billingx/evil", systems) == "/billingx/evil"           # prefix must match a path segment
    assert resolve_company_url("http://evil.example/billing", systems) == "http://evil.example/billing"
    assert resolve_company_url("/unknown/x", systems) == "/unknown/x"
