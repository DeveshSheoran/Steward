"""Whole stack, scripted agent: real worker + queue + runtime + gate + Chromium/Chrome + mock apps.
Only the LLM is replaced by a hand-written policy that reads the same tool results the real model would see."""
import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

from steward.app import build
from steward.config import Config
from steward.llm import ScriptedLLM, tu, tx


def find_ref(req, pattern):
    """Latest page snapshot in the conversation -> ref of the first element line matching `pattern`."""
    for m in reversed(req["messages"]):
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result" and "ELEMENTS:" in str(b.get("content")):
                for line in str(b["content"]).splitlines():
                    if re.search(pattern, line):
                        return re.match(r"\[(e\d+)\]", line).group(1)
    raise AssertionError(f"no element matching {pattern!r} in the latest snapshot")


def fill(name, value):    return lambda req: [tu("browser__fill", ref=find_ref(req, rf"name={name}\b"), value=value)]
def click(text):          return lambda req: [tu("browser__click", ref=find_ref(req, rf'"{text}"'))]
def select(name, value):  return lambda req: [tu("browser__select", ref=find_ref(req, rf"name={name}\b"), value=value)]
def goto(url):            return [tu("browser__goto", url=url)]


@pytest.fixture
def stack(tmp_path, mock_server):
    httpx.post(f"{mock_server}/__reset")
    pack = tmp_path / "pack"
    shutil.copytree("company_pack", pack)
    sy = pack / "systems.yaml"
    sy.write_text(sy.read_text().replace("127.0.0.1:8800", mock_server.split("//")[1]))
    cfg = Config(data_dir=tmp_path / "data", pack_dir=pack, files_dir=Path("company_files").resolve(), max_steps=40)

    def make(turns):
        return build(cfg, llm=ScriptedLLM(turns))
    return make, mock_server, tmp_path


PASS = dict(verdict="PASS", summary="all criteria directly observed",
            criteria=[dict(criterion="outcome", status="met", evidence="observed in the system")])


def refund_flow(base, ticket, order, amount, *, after_click):
    """Agent policy for the customer_refund SOP."""
    spec = dict(sop_id="customer_refund", intent=f"Refund INR {amount} on {order} for {ticket}", entities={"ticket": ticket, "order": order},
                success_criteria=[f"refund of INR {amount} recorded on order {order}"], plan=["read ticket", "refund", "reply"], blocking_questions=[])
    return [[tu("submit_task_spec", **spec)],
            goto(f"{base}/helpdesk/tickets/{ticket}"), goto(f"{base}/billing/orders/{order}"),
            fill("amount", str(amount)), fill("reason", f"Refund for {ticket}"), click("Issue refund"),
            *after_click]


def reply_and_finish(base, ticket, order):
    return [goto(f"{base}/helpdesk/tickets/{ticket}"), fill("body", "Hi, your refund has been issued. Thanks for your patience."),
            select("status", "resolved"), click("Send reply"),
            [tu("finish", summary=f"Refund issued on {order} and ticket {ticket} resolved.")],
            # verifier
            goto(f"{base}/billing/orders/{order}"), goto(f"{base}/helpdesk/tickets/{ticket}"), [tu("submit_verdict", **PASS)]]


def state(base):
    return httpx.get(f"{base}/__state").json()


def test_task_b_small_refund_runs_end_to_end(stack):
    make, base, _ = stack
    app = make(refund_flow(base, "T-501", "A-1001", 1500, after_click=reply_and_finish(base, "T-501", "A-1001")))
    tid = app.queue.enqueue("Customer Rohan Mehta wrote in about being charged twice (ticket T-501). Please sort it out.", "ops@northwind.example")
    assert app.worker("w1").run_once() == tid
    t = app.queue.get(tid)
    assert t["status"] == "completed", t["error"]
    st = state(base)
    assert st["orders"]["A-1001"]["refunded"] == 1500 and len(st["refunds"]) == 1
    assert st["tickets"]["T-501"]["status"] == "resolved" and "refund" in st["tickets"]["T-501"]["replies"][0]["body"]
    result = json.loads(t["result"])
    assert result["verdict"]["verdict"] == "PASS" and result["writes"] and result["report"]
    assert app.audit.verify().ok and app.evidence.verify(tid) == []


def test_task_b_large_refund_pauses_for_approval_and_resumes_in_the_background(stack):
    make, base, _ = stack
    app = make(refund_flow(base, "T-502", "A-1002", 4200, after_click=[
        # --- run resumes after the approval: the agent redoes the form exactly, then replies
        goto(f"{base}/billing/orders/A-1002"), fill("amount", "4200"), fill("reason", "Refund for T-502"), click("Issue refund"),
        *reply_and_finish(base, "T-502", "A-1002")]))
    tid = app.queue.enqueue("Ananya Rao's order arrived damaged (ticket T-502); she wants a refund.", "ops@northwind.example")
    w = app.worker("w1")
    assert w.run_once() == tid
    assert app.queue.get(tid)["status"] == "awaiting_approval"
    assert state(base)["orders"]["A-1002"]["refunded"] == 0                      # nothing reached the billing system
    assert w.run_once() is None                                                  # paused work does not hold a worker

    [pending] = app.gate.pending(tid)
    assert pending["approver"] == "finance-manager" and pending["args"]["amount"] == 4200
    app.gate.decide(pending["id"], approve=True, by="priya@northwind.example", comment="legit damage claim")

    assert w.run_once() == tid                                                   # worker notices the decision and resumes
    t = app.queue.get(tid)
    assert t["status"] == "completed", t["error"]
    st = state(base)
    assert st["orders"]["A-1002"]["refunded"] == 4200 and len(st["refunds"]) == 1
    kinds = [e["kind"] for e in app.audit.events(tid, limit=5000)]
    for k in ("approval.requested", "approval.approved", "task.paused", "task.resumed", "net.write", "verify.result", "task.completed"):
        assert k in kinds, k
    assert app.audit.verify().ok


def test_task_b_rejection_teaches_a_lesson_used_by_the_next_task(stack):
    make, base, _ = stack
    app = make([*refund_flow(base, "T-503", "A-1003", 18000, after_click=[]),
                [tu("escalate", reason="Finance rejected the refund; account manager must handle the enterprise customer")]])
    tid = app.queue.enqueue("Acme Logistics wants out of the annual plan (ticket T-503), refund them", "ops@northwind.example")
    w = app.worker("w1"); w.run_once()
    [pending] = app.gate.pending(tid)
    app.gate.decide(pending["id"], approve=False, by="priya@northwind.example", comment="Enterprise customers are handled by their account manager, never refunded from support")
    w.run_once()
    assert app.queue.get(tid)["status"] == "needs_human" and state(base)["orders"]["A-1003"]["refunded"] == 0
    # a later, similar request now carries the lesson into the agent's context
    ctx = app.memory.context_for("enterprise customer asks for a refund on their annual plan")
    assert any("account manager" in l["text"] for l in ctx["lessons"])


def test_task_c_weekly_report_uses_files_api_and_email_with_no_browser(stack):
    make, base, tmp = stack
    spec = dict(sop_id="weekly_ops_report", intent="Reconcile the orders export against Billing and email the Ops Lead",
                entities={}, success_criteria=["report lists every discrepancy"], plan=["read", "compare", "write", "email"], blocking_questions=[])
    report = "# Weekly ops report\nTotals: gross 24100, refunded 0, net 24100\nDiscrepancies:\n- A-1003 amount export 17500 vs billing 18000\n- A-1004 missing in billing\n"
    turns = [[tu("submit_task_spec", **spec)],
             [tu("files__read", path="exports/orders_export.csv")],
             [tu("files__summarize_table", path="exports/orders_export.csv", sum_columns=["amount", "refunded"])],
             [tu("http__get", url=f"{base}/billing/api/orders")],
             [tu("files__write", path="weekly_ops_report.md", content=report)],
             [tu("directory__lookup", query="operations")],
             [tu("outbox__send", to="meera.iyer@northwind.example", subject="Weekly ops report", body="2 discrepancies found; report attached in task evidence.")],
             [tu("finish", summary="Report written and emailed.")],
             [tu("files__read", path="out/weekly_ops_report.md")], [tu("outbox__list")], [tu("submit_verdict", **PASS)]]
    app = make(turns)
    tid = app.queue.enqueue("Prepare the weekly ops report: reconcile the orders export with billing and send it to the ops lead", "ops@northwind.example")
    app.worker("w1").run_once()
    t = app.queue.get(tid)
    assert t["status"] == "completed", t["error"]
    assert (app.cfg.workdir / "out" / tid / "weekly_ops_report.md").exists()
    mails = [json.loads(x) for x in (app.cfg.data_dir / "outbox.jsonl").read_text().splitlines()]
    assert mails[0]["to"] == "meera.iyer@northwind.example" and mails[0]["task_id"] == tid
    assert any(e["kind"] == "output" for e in app.evidence.list(tid)) and app.audit.verify().ok


def test_task_c_external_email_is_held_for_approval(stack):
    make, base, _ = stack
    spec = dict(sop_id="weekly_ops_report", intent="x", entities={}, success_criteria=["c"], plan=["p"], blocking_questions=[])
    app = make([[tu("submit_task_spec", **spec)], [tu("outbox__send", to="journalist@gmail.com", subject="s", body="b")]])
    tid = app.queue.enqueue("send the report to a journalist", "ops@northwind.example")
    app.worker("w").run_once()
    assert app.queue.get(tid)["status"] == "awaiting_approval"
    assert not (app.cfg.data_dir / "outbox.jsonl").exists()
