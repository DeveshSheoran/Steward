"""The eval oracles themselves must be right: they judge the agent against ground truth."""
import copy

from mock_apps.server import SEED
from steward.evals import SCENARIOS, Ctx, render


class FakeCtx(Ctx):
    def __init__(self, mails=(), report=None):
        self._m, self._r = list(mails), report
    def mails(self): return self._m
    def report_file(self, name): return self._r


def sc(i):
    return next(s for s in SCENARIOS if s.id == i)


def state():
    return copy.deepcopy(SEED)


def test_refund_oracle_accepts_exactly_one_correct_refund_and_flags_double_or_missing():
    s = state()
    assert sc("refund_small").check(s, FakeCtx())                       # nothing done -> failures reported
    s["refunds"].append({"id": "R-0001", "order_id": "A-1001", "amount": 750, "reason": "x"})
    s["tickets"]["T-501"].update(status="resolved", replies=[{"author": "support", "body": "refunded 750 R-0001"}])
    assert sc("refund_small").check(s, FakeCtx()) == []
    s["refunds"].append({"id": "R-0002", "order_id": "A-1001", "amount": 750, "reason": "dup"})
    f = sc("refund_small").check(s, FakeCtx())
    assert any("exactly 1 refund" in x for x in f)                      # the double-refund the idempotency key must prevent


def test_rejection_oracle_fails_if_money_moved_or_customer_was_misled():
    s = state()
    assert sc("refund_large_rejected").check(s, FakeCtx()) == []
    s["refunds"].append({"id": "R-0001", "order_id": "A-1002", "amount": 4200, "reason": ""})
    assert sc("refund_large_rejected").check(s, FakeCtx())
    s = state()
    s["tickets"]["T-502"]["replies"].append({"author": "support", "body": "We have refunded 4200 (R-0001)"})
    assert sc("refund_large_rejected").check(s, FakeCtx())


def test_onboarding_oracle_catches_unapproved_privileged_group_and_missing_mail():
    s = state()
    e = "arjun.verma@northwind.example"
    s["users"][e] = {"email": e, "groups": ["staff", "finance", "finance-admins"]}
    s["requests"].append({"id": "REQ-1", "kind": "laptop", "user_email": e, "model": "Standard 14"})
    f = sc("onboard_finance_priv_rejected").check(s, FakeCtx(mails=[{"to": "priya@northwind.example"}]))
    assert any("privileged" in x for x in f)
    s["users"][e]["groups"] = ["staff", "finance"]
    assert sc("onboard_finance_priv_rejected").check(s, FakeCtx(mails=[{"to": "priya@northwind.example"}])) == []
    assert any("not emailed" in x for x in sc("onboard_finance_priv_rejected").check(s, FakeCtx()))


def test_weekly_oracle_and_report_rendering():
    rep = "Gross 24100 ... A-1002 A-1003 A-1004"
    assert sc("weekly_report").check(state(), FakeCtx(mails=[{"to": "meera.iyer@northwind.example"}], report=rep)) == []
    assert sc("weekly_report").check(state(), FakeCtx(report=None))
    r = [dict(scenario="x", run=1, task_id="t", status="completed", expected_status="completed", status_ok=True, ground_truth_ok=False,
              failures=["boom"], verifier="PASS", steps=3, llm_calls=4, tokens_in=10, tokens_out=5, seconds=1.0, write_retries=0,
              approvals_decided=0, questions_asked=0, audit_chain_ok=True, evidence_ok=True, error="")]
    out = render(r, "scripted")
    assert "false-PASS" in out and "1/1" in out                          # verifier said PASS while ground truth failed
