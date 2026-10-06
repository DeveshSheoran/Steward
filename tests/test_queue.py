import json

import pytest

from steward.audit import AuditLog
from steward.llm import FatalRunError, TransientRunError
from steward.queue import TaskQueue
from steward.runtime import RunOutcome
from steward.store import Store
from steward.worker import Worker


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t


@pytest.fixture
def q(tmp_path):
    store = Store(tmp_path / "q.db")
    clock = Clock()
    return TaskQueue(store, AuditLog(store), clock=clock, backoff=lambda n: 10.0 * n), clock, store


def test_claim_is_exclusive_and_priority_ordered(q):
    queue, clock, _ = q
    a = queue.enqueue("low")
    b = queue.enqueue("high", priority=5)
    assert queue.claim("w1")["id"] == b
    assert queue.claim("w2")["id"] == a
    assert queue.claim("w3") is None                      # nothing left; running tasks are not handed out twice


def test_scheduled_task_waits_for_its_time(q):
    queue, clock, _ = q
    tid = queue.enqueue("later", run_at=clock.t + 60)
    assert queue.claim("w") is None
    clock.t += 61
    assert queue.claim("w")["id"] == tid


def test_retry_with_backoff_then_dead_letter(q):
    queue, clock, _ = q
    tid = queue.enqueue("flaky", max_attempts=3)
    queue.claim("w")
    assert queue.fail_transient(tid, "503") == "queued"
    assert queue.claim("w") is None                       # backoff: not yet
    clock.t += 10
    queue.claim("w")
    assert queue.fail_transient(tid, "503") == "queued"
    clock.t += 20
    queue.claim("w")
    assert queue.fail_transient(tid, "503") == "dead"
    t = queue.get(tid)
    assert t["status"] == "dead" and t["attempts"] == 3 and t["error"] == "503"


def test_crashed_worker_lease_is_reclaimed(q):
    queue, clock, _ = q
    tid = queue.enqueue("x")
    queue.claim("w1", lease_s=30)
    clock.t += 31                                          # w1 died: no heartbeat
    got = queue.claim("w2")
    assert got["id"] == tid and queue.get(tid)["attempts"] == 1


def test_heartbeat_keeps_lease(q):
    queue, clock, _ = q
    tid = queue.enqueue("x")
    queue.claim("w1", lease_s=30)
    clock.t += 20
    assert queue.heartbeat(tid, "w1", 30)
    clock.t += 20
    assert queue.claim("w2") is None
    assert not queue.heartbeat(tid, "someone-else", 30)


def test_paused_task_resumes_only_after_approval_decided(q):
    queue, clock, store = q
    tid = queue.enqueue("needs approval")
    queue.claim("w")
    store.execute("INSERT INTO approvals(id, task_id, fingerprint, tool, args, risk, reasons, status, created_at) VALUES ('a1',?,?,?,?,?,?,?,?)",
                  (tid, "fp", "http.request", "{}", 3, "[]", "pending", clock.t))
    queue.finish(tid, "awaiting_approval")
    assert queue.resume_ready() == [] and queue.claim("w") is None
    store.execute("UPDATE approvals SET status='approved' WHERE id='a1'")
    assert queue.resume_ready() == [tid]
    assert queue.claim("w")["id"] == tid


class FakeRuntime:
    def __init__(self, behaviours): self.b = behaviours
    def run(self, tid):
        x = self.b.pop(0)
        if isinstance(x, Exception):
            raise x
        return x


def test_worker_maps_outcomes_and_retries_transient_llm_errors(q):
    queue, clock, _ = q
    tid = queue.enqueue("t", max_attempts=3)
    rt = FakeRuntime([TransientRunError("overloaded"), RunOutcome("completed", "ok", {"status": "completed"})])
    w = Worker(queue, lambda: rt, "w1")
    assert w.run_once() == tid and queue.get(tid)["status"] == "queued"
    assert w.run_once() is None                            # still backing off
    clock.t += 10
    assert w.run_once() == tid
    t = queue.get(tid)
    assert t["status"] == "completed" and json.loads(t["result"])["status"] == "completed" and t["attempts"] == 1


def test_worker_dead_letters_a_task_that_keeps_crashing(q):
    queue, clock, _ = q
    tid = queue.enqueue("bug", max_attempts=2)
    w = Worker(queue, lambda: FakeRuntime([RuntimeError("boom"), RuntimeError("boom")]), "w1")
    w.run_once(); clock.t += 100; w.run_once()
    t = queue.get(tid)
    assert t["status"] == "dead" and "boom" in t["error"]


def test_fatal_llm_errors_fail_fast_without_burning_retries(q):
    queue, clock, _ = q
    tid = queue.enqueue("t", max_attempts=3)
    w = Worker(queue, lambda: FakeRuntime([FatalRunError("Anthropic API rejected the request (BadRequestError): Your credit balance is too low")]), "w1")
    w.run_once()
    t = queue.get(tid)
    assert t["status"] == "dead" and t["attempts"] == 0 and "credit balance" in t["error"]


def test_quota_defer_parks_task_without_burning_attempts():
    from steward.llm import QuotaExhausted, parse_reset_seconds
    import datetime as dt, zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now = dt.datetime(2026, 10, 6, 12, 26, tzinfo=tz)
    secs = parse_reset_seconds("You've hit your session limit · resets 3:20pm (Asia/Kolkata)", now=now)
    assert 2.9 * 3600 < secs < 3.1 * 3600                    # ~2h54m + 60s margin
    assert parse_reset_seconds("no time here") == 1800.0
    assert issubclass(QuotaExhausted, Exception)


def test_worker_defers_on_quota_and_resumes_after_reset_without_burning_attempts(q):
    from steward.llm import QuotaExhausted
    queue, clock, _ = q
    tid = queue.enqueue("t", max_attempts=2)
    rt = FakeRuntime([QuotaExhausted("session limit", 3 * 3600), QuotaExhausted("session limit", 3 * 3600),
                      RunOutcome("completed", "ok", {"status": "completed"})])
    w = Worker(queue, lambda: rt, "w1")
    w.run_once()
    t = queue.get(tid)
    assert t["status"] == "queued" and t["attempts"] == 0 and t["run_at"] == clock.t + 3 * 3600
    assert w.run_once() is None                            # nothing happens until the quota resets
    clock.t += 3 * 3600 + 1
    w.run_once()                                           # still limited: deferred again, still alive (max_attempts=2 not consumed)
    assert queue.get(tid)["status"] == "queued" and queue.get(tid)["attempts"] == 0
    clock.t += 3 * 3600 + 1
    w.run_once()
    assert queue.get(tid)["status"] == "completed"
