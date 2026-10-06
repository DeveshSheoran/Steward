"""Runtime tests with a scripted LLM and fake tools: no network, no browser."""
import json

import pytest
import yaml

from steward.audit import AuditLog, EvidenceStore
from steward.config import Config
from steward.gate import Gate
from steward.llm import ScriptedLLM, tu, tx
from steward.memory import CompanyPack, Memory
from steward.policy import PolicyEngine
from steward.runtime import Runtime
from steward.store import Store, now
from steward.tools.base import ErrorKind, Risk, Tool, ToolRegistry, ToolResult

SPEC = dict(sop_id="refund", intent="Refund the duplicate charge on A-1001", entities={"order": "A-1001"},
            success_criteria=["refund of INR 1500 is recorded on order A-1001"], plan=["open order", "refund"], blocking_questions=[])


class World:
    """Fake company system the fake tools read/write."""
    def __init__(self):
        self.refunds, self.get_calls, self.req_calls, self.fail_gets = [], 0, 0, 0


@pytest.fixture
def rig(tmp_path):
    (tmp_path / "pack" / "sops").mkdir(parents=True)
    (tmp_path / "pack" / "sops" / "refund.md").write_text(
        "---\nid: refund\ntitle: Customer refund\ntriggers: [refund, charged twice]\nsystems: [billing]\n"
        "done_when: [customer is told the outcome]\n---\n1. Open the order. 2. Refund per policy.\n")
    (tmp_path / "pack" / "systems.yaml").write_text(yaml.dump({"systems": {"billing": {"description": "Billing", "base_url": "http://x/billing"}}}))
    pack = CompanyPack(tmp_path / "pack")
    store = Store(tmp_path / "s.db"); audit = AuditLog(store)
    engine = PolicyEngine.from_file("company_pack/policies.yaml")
    gate = Gate(store, audit, engine)
    world = World()

    def h_get(ctx, a):
        world.get_calls += 1
        if world.fail_gets > 0:
            world.fail_gets -= 1
            return ToolResult(False, error="timeout", error_kind=ErrorKind.TRANSIENT)
        return ToolResult(True, json.dumps({"refunds": world.refunds}))

    def h_req(ctx, a):
        world.req_calls += 1
        if a.get("fail"):
            return ToolResult(False, error="503", error_kind=ErrorKind.TRANSIENT)
        world.refunds.append(a["amount"])
        return ToolResult(True, f"refund of {a['amount']} recorded")

    reg = ToolRegistry()
    obj = lambda *req: {"type": "object", "properties": {k: {"type": "string"} for k in req}, "required": list(req)}
    reg.register(Tool("http.get", "read", obj("url"), Risk.R0_READ, h_get, system="billing"))
    reg.register(Tool("http.request", "write", {"type": "object", "properties": {"order": {}, "amount": {}}, "required": ["order", "amount"]},
                      Risk.R3_CRITICAL, h_req, system="billing", mutating=True))
    reg.register(Tool("shell.run", "nope", obj("cmd"), Risk.R0_READ, lambda c, a: ToolResult(True, "x")))

    def build(turns, **kw):
        cfg = Config(data_dir=tmp_path / "data", model="m", verifier_model="v", max_steps=kw.pop("max_steps", 12))
        llm = ScriptedLLM(turns)
        return Runtime(cfg=cfg, store=store, audit=audit, evidence=EvidenceStore(store, audit, tmp_path / "ev"), gate=gate,
                       memory=Memory(store, pack), registry=reg, llm=llm, retry_sleep=0, **kw), llm

    def task(goal="Customer Rohan says he was charged twice on order A-1001, please sort it out"):
        tid = f"t{store.one('SELECT COUNT(*) c FROM tasks')['c'] + 1}"
        store.execute("INSERT INTO tasks(id, goal, requester, run_at, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                      (tid, goal, "ops@northwind.example", now(), now(), now()))
        return tid

    return type("Rig", (), dict(build=build, task=task, store=store, audit=audit, gate=gate, world=world, pack=pack))


def kinds(rig, tid):
    return [e["kind"] for e in rig.audit.events(tid, limit=5000)]


def assert_pairing(messages):
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and isinstance(m["content"], list):
            ids = [b["id"] for b in m["content"] if b["type"] == "tool_use"]
            if ids:
                assert messages[i + 1]["role"] == "user", "tool_use must be followed by a user turn"
                got = {b["tool_use_id"] for b in messages[i + 1]["content"] if isinstance(b, dict) and b.get("type") == "tool_result"}
                assert set(ids) <= got, f"unanswered tool_use ids {set(ids) - got}"


PASS = dict(verdict="PASS", criteria=[dict(criterion="refund recorded", status="met", evidence="saw refund 1500")], summary="all met")
FAIL = dict(verdict="FAIL", criteria=[dict(criterion="refund recorded", status="not_met", evidence="no refund in billing")], summary="refund missing")


def refund_turns(amount=1500):
    return [[tx("Refunding."), tu("http__request", order="A-1001", amount=amount)], [tu("finish", summary=f"refunded {amount}")]]


def test_happy_path_with_independent_verification_and_report(rig):
    tid = rig.task()
    rt, llm = rig.build([[tu("submit_task_spec", **SPEC)], [tx("Look first."), tu("http__get", url="/orders/A-1001")], *refund_turns(),
                         [tu("http__get", url="/orders/A-1001")], [tu("submit_verdict", **PASS)]])
    out = rt.run(tid)
    assert out.status == "completed" and rig.world.refunds == [1500]
    k = kinds(rig, tid)
    assert "task.understood" in k and "verify.result" in k and "task.completed" in k and k.count("policy.decision") >= 3
    assert out.result["verdict"]["verdict"] == "PASS" and out.result["report"]
    # SOP done_when was merged into the criteria the verifier saw
    ver_req = json.loads(llm.calls[-2]["messages"][0]["content"])
    assert "customer is told the outcome" in ver_req["success_criteria"]
    # verifier only gets read-only tools (+ submit_verdict)
    assert {t["name"] for t in llm.calls[-1]["tools"]} == {"http__get", "submit_verdict"}
    assert rig.audit.verify().ok


def test_verification_failure_sends_agent_back_to_fix(rig):
    tid = rig.task()
    rt, llm = rig.build([[tu("submit_task_spec", **SPEC)],
                         [tu("finish", summary="done (claimed, but I did nothing)")], [tu("submit_verdict", **FAIL)],
                         *refund_turns(), [tu("submit_verdict", **PASS)]])
    out = rt.run(tid)
    assert out.status == "completed" and rig.world.refunds == [1500]
    resumed_user = llm.calls[3]["messages"][-1]["content"]
    assert any("VERIFICATION FAIL" in b.get("text", "") for b in resumed_user if isinstance(b, dict))
    assert_pairing(json.loads(rig.store.one("SELECT state FROM run_state WHERE task_id=?", (tid,))["state"])["messages"])


def test_repeated_verification_failure_escalates_instead_of_claiming_success(rig):
    tid = rig.task()
    rt, _ = rig.build([[tu("submit_task_spec", **SPEC)], [tu("finish", summary="x")], [tu("submit_verdict", **FAIL)],
                       [tu("finish", summary="x")], [tu("submit_verdict", **FAIL)], [tu("finish", summary="x")], [tu("submit_verdict", **FAIL)]])
    out = rt.run(tid)
    assert out.status == "needs_human" and "Verification did not pass" in out.summary and rig.world.refunds == []


def test_policy_pause_then_resume_after_approval(rig):
    tid = rig.task()
    rt, llm = rig.build([[tu("submit_task_spec", **SPEC)], *refund_turns(2500)[:1]])
    out = rt.run(tid)
    assert out.status == "awaiting_approval" and rig.world.refunds == [] and len(out.approval_ids) == 1
    assert rt.run(tid).status == "awaiting_approval"                       # still undecided: stays paused, no LLM call
    assert len(llm.calls) == 2

    rig.gate.decide(out.approval_ids[0], approve=True, by="finance-manager", comment="ok")
    seen = {}
    def after_resume(req):
        seen["last_user"] = req["messages"][-1]["content"]
        return [tu("http__request", order="A-1001", amount=2500)]
    def verifier_turn(req):
        seen["ver"] = json.loads(req["messages"][0]["content"])
        return [tu("submit_verdict", **PASS)]
    rt2, llm2 = rig.build([after_resume, [tu("finish", summary="refunded 2500")], verifier_turn])
    out2 = rt2.run(tid)
    ap = seen["ver"]["approvals_recorded_by_the_gate"]                     # the verifier can check "a human approved this"
    assert len(ap) == 1 and ap[0]["status"] == "consumed" and ap[0]["decided_by"] == "finance-manager" and ap[0]["args"]["amount"] == 2500
    assert out2.status == "completed" and rig.world.refunds == [2500]       # applied exactly once, via the grant
    assert any("RESUMED" in b.get("text", "") and "GRANTED" in b.get("text", "") for b in seen["last_user"] if isinstance(b, dict))
    assert_pairing(json.loads(rig.store.one("SELECT state FROM run_state WHERE task_id=?", (tid,))["state"])["messages"])
    assert rig.audit.verify().ok


def test_rejection_becomes_a_lesson_and_agent_escalates(rig):
    tid = rig.task()
    rt, _ = rig.build([[tu("submit_task_spec", **SPEC)], *refund_turns(2500)[:1]])
    out = rt.run(tid)
    rig.gate.decide(out.approval_ids[0], approve=False, by="finance-manager", comment="Enterprise accounts go via the account manager")
    seen = {}
    def after(req):
        seen["u"] = json.dumps(req["messages"][-1]["content"])
        return [tu("escalate", reason="Refund rejected by finance-manager; needs account manager")]
    out2 = rig.build([after])[0].run(tid)
    assert "REJECTED" in seen["u"] and "account manager" in seen["u"]
    assert out2.status == "needs_human" and rig.world.refunds == []
    les = rig.store.query("SELECT * FROM lessons")
    assert len(les) == 1 and les[0]["scope"] == "sop:refund" and "account manager" in les[0]["text"]
    # next time a similar task is understood, the lesson is in the context handed to the agent
    ctx = Memory(rig.store, rig.pack).context_for("customer wants a refund on order A-1003")
    assert any("account manager" in l["text"] for l in ctx["lessons"])


def test_hard_denial_is_not_escalated_to_a_human_and_agent_must_adapt(rig):
    tid = rig.task()
    rt, llm = rig.build([[tu("submit_task_spec", **SPEC)], [tu("http__request", order="A-1001", amount=80000)],
                         [tu("escalate", reason="policy forbids refunds above 50000")]])
    out = rt.run(tid)
    assert out.status == "needs_human" and rig.world.refunds == [] and rig.world.req_calls == 0
    denied = json.dumps(llm.calls[2]["messages"][-1]["content"])
    assert "DENIED" in denied and "FIN-9" in denied
    assert not rig.gate.pending(tid)


def test_unknown_or_forbidden_tool_is_refused(rig):
    tid = rig.task()
    rt, llm = rig.build([[tu("submit_task_spec", **SPEC)], [tu("shell__run", cmd="rm -rf /")], [tu("escalate", reason="no shell")]])
    rt.run(tid)
    assert "Unknown or not-permitted" in json.dumps(llm.calls[2]["messages"][-1]["content"])
    assert "shell__run" not in {t["name"] for t in llm.calls[1]["tools"]}      # never even shown to the model


def test_blocking_question_at_intake_then_answer(rig):
    tid = rig.task("Refund the customer")
    spec_q = dict(SPEC, blocking_questions=["Which order should be refunded?"])
    seen = {}
    def second_spec(req):
        seen["u"] = req["messages"][0]["content"]
        return [tu("submit_task_spec", **SPEC)]
    rt, _ = rig.build([[tu("submit_task_spec", **spec_q)]])
    out = rt.run(tid)
    assert out.status == "waiting_input" and "Which order" in out.question
    rt.provide_answer(tid, "A-1001, the duplicate October charge")
    rt2, _ = rig.build([second_spec, *refund_turns(), [tu("submit_verdict", **PASS)]])
    assert rt2.run(tid).status == "completed"
    assert "A-1001, the duplicate October charge" in seen["u"]


def test_ask_human_mid_task_returns_answer_as_tool_result(rig):
    tid = rig.task()
    rt, _ = rig.build([[tu("submit_task_spec", **SPEC)], [tx("Unsure of amount."), tu("note", key="order", value="A-1001"), tu("ask_human", question="How much to refund?")]])
    out = rt.run(tid)
    assert out.status == "waiting_input" and out.question == "How much to refund?"
    rt.provide_answer(tid, "1500")
    seen = {}
    def next_turn(req):
        seen["u"] = json.dumps(req["messages"][-1]["content"])
        return [tu("http__request", order="A-1001", amount=1500)]
    def verifier_turn(req):
        seen["v"] = json.loads(req["messages"][0]["content"])
        return [tu("submit_verdict", **PASS)]
    rt2, _ = rig.build([next_turn, [tu("finish", summary="done")], verifier_turn])
    assert rt2.run(tid).status == "completed" and "Human answered: 1500" in seen["u"]
    # the verifier is told what the requester clarified, so corrected requests are judged by the corrected intent
    assert seen["v"]["clarifications_from_requester"][0]["answer"] == "1500"
    assert_pairing(json.loads(rig.store.one("SELECT state FROM run_state WHERE task_id=?", (tid,))["state"])["messages"])
    assert Memory(rig.store, rig.pack).notes(tid) == {"order": "A-1001"}


def test_transient_read_errors_are_retried_invisibly(rig):
    tid = rig.task()
    rig.world.fail_gets = 2
    rt, llm = rig.build([[tu("submit_task_spec", **SPEC)], [tu("http__get", url="/o")], [tu("escalate", reason="stop")]])
    rt.run(tid)
    assert rig.world.get_calls == 3 and kinds(rig, tid).count("tool.retry") == 2
    assert "timeout" not in json.dumps(llm.calls[2]["messages"][-1]["content"])


def test_mutating_tools_are_never_blindly_retried(rig):
    tid = rig.task()
    rt, _ = rig.build([[tu("submit_task_spec", **SPEC)], [tu("http__request", order="A-1001", amount=100, fail=True)], [tu("escalate", reason="write failed")]])
    rt.run(tid)
    assert rig.world.req_calls == 1


def test_step_budget_stops_runaway_loops(rig):
    tid = rig.task()
    rt, _ = rig.build([[tu("submit_task_spec", **SPEC)]] + [[tu("http__get", url="/o")] for _ in range(5)], max_steps=3)
    out = rt.run(tid)
    assert out.status == "needs_human" and "Step budget" in out.summary
