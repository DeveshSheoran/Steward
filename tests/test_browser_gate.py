"""End-to-end (no LLM): real Chromium -> mock Billing/Helpdesk apps, with the network-level gate in between."""
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from mock_apps.server import app as mock_app
from steward.audit import AuditLog, EvidenceStore
from steward.config import Config
from steward.gate import Gate
from steward.policy import PolicyEngine
from steward.store import Store
from steward.tools.base import ToolContext
from steward.tools.browser import BrowserSession


@pytest.fixture(scope="module")
def server():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


@pytest.fixture
def env(server, tmp_path):
    httpx.post(f"{server}/__reset")
    store = Store(tmp_path / "s.db"); audit = AuditLog(store)
    gate = Gate(store, audit, PolicyEngine.from_file("company_pack/policies.yaml"))
    ev = EvidenceStore(store, audit, tmp_path / "ev")
    systems = {
        "billing": {"base_url": f"{server}/billing/orders", "prefix": "/billing",
                    "mutations": [{"match": "POST /billing/refunds", "risk": "R3", "label": "Issue a refund"}]},
        "helpdesk": {"base_url": f"{server}/helpdesk/tickets", "prefix": "/helpdesk",
                     "mutations": [{"match": "POST /helpdesk/tickets/*/reply", "risk": "R2", "label": "Reply to customer"}]},
    }
    cfg = Config()   # reads STEWARD_BROWSER_CHANNEL etc. from env / .env (e.g. chrome on Ubuntu 20.04)
    sess = BrowserSession(cfg=cfg, gate=gate, audit=audit, evidence=ev, systems=systems, task_id="t1", role="ops-agent")
    sess.start()
    ctx = ToolContext("t1", store, audit, ev, str(tmp_path), {})
    yield SimpleNamespace(base=server, sess=sess, gate=gate, audit=audit, ctx=ctx, ev=ev)
    sess.close()


def ref(sess, *, name=None, text=None):
    for e in sess.snapshot()["elements"]:
        if (name and e["name"] == name) or (text and text.lower() in (e["text"] or "").lower()):
            return e["ref"]
    raise AssertionError(f"no element name={name} text={text}")


def state(base):
    return httpx.get(f"{base}/__state").json()


def refund(env, order, amount):
    s = env.sess
    s.goto(env.ctx, f"{env.base}/billing/orders/{order}")
    s.fill(env.ctx, ref(s, name="amount"), str(amount))
    s.fill(env.ctx, ref(s, name="reason"), "test")
    return s.click(env.ctx, ref(s, text="refund"))


def test_small_refund_is_allowed_and_leaves_evidence(env):
    r = refund(env, "A-1001", 1500)
    assert r.ok and "WRITES APPLIED" in r.output and r.evidence
    assert state(env.base)["orders"]["A-1001"]["refunded"] == 1500
    assert env.ev.verify("t1") == [] and env.audit.verify().ok


def test_large_refund_is_blocked_then_goes_through_after_approval(env):
    r = refund(env, "A-1002", 2500)
    assert not r.ok and r.signal == "pause_for_approval" and r.meta["approver"] == "finance-manager"
    assert state(env.base)["orders"]["A-1002"]["refunded"] == 0              # nothing was sent to the app
    env.gate.decide(r.meta["approval_id"], approve=True, by="finance-manager", comment="ok")
    r2 = refund(env, "A-1002", 2500)                                           # agent redoes the form after resume
    assert r2.ok and state(env.base)["orders"]["A-1002"]["refunded"] == 2500
    r3 = refund(env, "A-1002", 2500)                                           # grant was single-use
    assert r3.signal == "pause_for_approval"


def test_hard_cap_is_denied_not_escalated(env):
    r = refund(env, "A-1003", 60000)
    assert not r.ok and r.signal is None and "DENIED" in r.error
    assert state(env.base)["orders"]["A-1003"]["refunded"] == 0
    assert not env.gate.pending("t1")


def test_transient_503_is_retried_transparently(env):
    httpx.post(f"{env.base}/__chaos", json={"fail_next_mutations": 2})
    r = refund(env, "A-1001", 500)
    assert r.ok and "attempts=3" in r.output
    assert len(state(env.base)["refunds"]) == 1


def test_503_after_apply_does_not_double_apply(env):
    httpx.post(f"{env.base}/__chaos", json={"fail_after_apply": 1})
    r = refund(env, "A-1001", 700)
    assert r.ok and "attempts=2" in r.output
    st = state(env.base)
    assert len(st["refunds"]) == 1 and st["orders"]["A-1001"]["refunded"] == 700     # idempotency key worked


def test_persistent_failure_is_reported_not_claimed_as_success(env):
    httpx.post(f"{env.base}/__chaos", json={"fail_next_mutations": 99})
    r = refund(env, "A-1001", 100)
    assert not r.ok and r.error_kind == "transient" and "NOT confirmed" in r.error
    assert state(env.base)["refunds"] == []


def test_offsite_navigation_is_blocked(env):
    r = env.sess.goto(env.ctx, "http://example.com/")
    assert not r.ok
    assert any(e["kind"] == "net.blocked" for e in env.audit.events("t1"))
