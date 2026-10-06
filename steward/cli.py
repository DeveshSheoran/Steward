"""Command line: python -m steward <command>. `run` is the interactive demo/debug entry point."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import threading
import time

from .app import build

TERMINAL = {"completed", "needs_human", "dead"}


def fmt_event(e: dict) -> str | None:
    k, p = e["kind"], e["payload"]
    short = lambda s, n=110: (str(s)[:n] + "...") if len(str(s)) > n else str(s)
    if k == "task.understood":
        return f"  understood  sop={p.get('sop_id')}  intent: {short(p.get('intent'))}\n              criteria: {short(p.get('success_criteria'), 200)}"
    if k == "agent.message":
        return f"  [{e['actor']}] {short(p['text'], 160)}"
    if k == "tool.call":
        return f"  -> {p['tool']} {short(json.dumps(p['args'], ensure_ascii=False), 100)}"
    if k == "tool.result":
        return f"     {'ok ' if p['ok'] else 'ERR'} {short(p.get('error') or p.get('preview'), 90)}"
    if k == "policy.decision" and (p["verdict"] != "allow" or p["risk"] in ("R2_EXTERNAL", "R3_CRITICAL")):
        return f"  policy: {p['verdict'].upper()}  {p['tool']} risk={p['risk']} {short('; '.join(p['reasons']), 100)}"
    if k == "net.write":
        return f"  WRITE applied: {p['label']} HTTP {p['status']} (attempts {p['attempts']})"
    if k == "net.retry":
        return f"  write retry {p['attempt']}: {p['error']}"
    if k in ("approval.requested", "task.paused", "task.resumed", "human.question", "human.answer", "task.completed", "task.needs_human",
             "task.retry_scheduled", "task.dead"):
        return f"  == {k} {short(json.dumps(p, ensure_ascii=False), 140)}"
    if k == "verify.result":
        return f"  VERIFY {p['verdict']}: {short(p['summary'], 120)}"
    return None


def drain(app, tid: str, after: int) -> int:
    for e in app.audit.events(tid, after_seq=after, limit=5000):
        line = fmt_event(e)
        if line:
            print(line, flush=True)
        after = e["seq"]
    return after


def cmd_run(app, a) -> int:
    llm = app.get_llm()                      # fail early, with a readable message, if the backend is not set up
    tid = app.queue.enqueue(a.goal, a.requester)
    print(f"LLM backend: {getattr(llm, 'name', type(llm).__name__)}\ntask {tid} queued\n")
    worker, seen = app.worker("cli"), 0
    while True:
        worker.run_once()
        seen = drain(app, tid, seen)
        t = app.queue.get(tid)
        if t["status"] == "awaiting_approval":
            for p in app.gate.pending(tid):
                print(f"\nAPPROVAL NEEDED ({p['approver'] or 'any approver'}): {p['tool']} {json.dumps(p['args'])}\n  because: {'; '.join(p['reasons'])}")
                ok = input("  Approve? [y/N] ").strip().lower() == "y"
                c = input("  Comment (optional): ").strip() or None
                app.gate.decide(p["id"], approve=ok, by=a.approver, comment=c)
            continue
        if t["status"] == "waiting_input":
            print(f"\nQUESTION: {json.loads(t['result'])['question']}")
            app.runtime_factory().provide_answer(tid, input("  Your answer: ").strip())
            app.queue.requeue(tid, "answered")
            continue
        if t["status"] == "queued":
            wait = (t["run_at"] or 0) - time.time()
            if wait > 120:                            # deferred (plan limit): don't sit here for hours
                when = dt.datetime.fromtimestamp(t["run_at"]).strftime("%H:%M")
                print(f"\nPAUSED: {t['error'] or 'deferred'}\nThe task keeps its progress and is scheduled for {when}. "
                      f"Run `python -m steward worker` after that time (or leave a worker running) to resume it.")
                return 3
            time.sleep(1)
            continue
        break
    t = app.queue.get(tid)
    print(f"\nFINAL STATUS: {t['status']}" + (f"  ({t['error']})" if t["error"] else ""))
    r = json.loads(t["result"] or "{}")
    if r.get("report"):
        rep = next(e for e in app.evidence.list(tid) if e["id"] == r["report"])
        print("\n" + open(rep["path"]).read())
    return 0 if t["status"] == "completed" else 1


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="steward")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("run", help="queue a task and run it interactively (live timeline, asks you for approvals)")
    p.add_argument("goal"); p.add_argument("--requester", default="ops@northwind.example"); p.add_argument("--approver", default="you")
    p = sp.add_parser("submit", help="queue a task for a background worker")
    p.add_argument("goal"); p.add_argument("--requester", default="ops@northwind.example")
    p.add_argument("--at", help="run later, local time 'YYYY-MM-DD HH:MM'")
    p = sp.add_parser("worker", help="run a background worker"); p.add_argument("--once", action="store_true")
    sp.add_parser("list", help="list tasks")
    p = sp.add_parser("status"); p.add_argument("task_id")
    sp.add_parser("approvals", help="list pending approvals")
    p = sp.add_parser("approve"); p.add_argument("approval_id"); p.add_argument("--by", default="you"); p.add_argument("--comment")
    p = sp.add_parser("reject"); p.add_argument("approval_id"); p.add_argument("--by", default="you"); p.add_argument("--comment", required=True)
    p = sp.add_parser("answer"); p.add_argument("task_id"); p.add_argument("text")
    p = sp.add_parser("audit", help="timeline of a task"); p.add_argument("task_id")
    p = sp.add_parser("report"); p.add_argument("task_id")
    sp.add_parser("verify-audit", help="check the audit hash chain and evidence hashes")
    p = sp.add_parser("eval", help="run evaluation scenarios against the mock apps and score them against ground truth")
    p.add_argument("--only", help="comma separated scenario ids"); p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--timeout", type=int, default=900, help="seconds per run"); p.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "eval":
        from .evals import main as eval_main
        sys.exit(eval_main(a))
    app = build()

    if a.cmd == "run":
        sys.exit(cmd_run(app, a))
    elif a.cmd == "submit":
        at = dt.datetime.strptime(a.at, "%Y-%m-%d %H:%M").timestamp() if a.at else None
        print(app.queue.enqueue(a.goal, a.requester, run_at=at))
    elif a.cmd == "worker":
        w = app.worker()
        if a.once:
            print(w.run_once() or "idle")
        else:
            print("worker running (Ctrl+C to stop)")
            try:
                w.run_forever()
            except KeyboardInterrupt:
                pass
    elif a.cmd == "list":
        for t in app.queue.list():
            print(f"{t['id']}  {t['status']:<18} {t['goal'][:70]}")
    elif a.cmd == "status":
        print(json.dumps(app.queue.get(a.task_id), indent=1, default=str))
    elif a.cmd == "approvals":
        for p in app.gate.pending():
            print(f"{p['id']}  task={p['task_id']}  {p['tool']} {json.dumps(p['args'])}  approver={p['approver']}  ({'; '.join(p['reasons'])})")
    elif a.cmd in ("approve", "reject"):
        print(app.gate.decide(a.approval_id, approve=a.cmd == "approve", by=a.by, comment=a.comment))
    elif a.cmd == "answer":
        app.runtime_factory().provide_answer(a.task_id, a.text)
        app.queue.requeue(a.task_id, "answered")
    elif a.cmd == "audit":
        drain(app, a.task_id, 0)
    elif a.cmd == "report":
        t = app.queue.get(a.task_id)
        r = json.loads(t["result"] or "{}")
        rep = next((e for e in app.evidence.list(a.task_id) if e["id"] == r.get("report")), None)
        print(open(rep["path"]).read() if rep else "no report yet")
    elif a.cmd == "verify-audit":
        v = app.audit.verify()
        print(f"audit chain: {'OK' if v.ok else 'BROKEN at seq ' + str(v.broken_at_seq) + ' - ' + str(v.reason)} ({v.checked} events)")
        bad = app.evidence.verify()
        print(f"evidence: {'OK' if not bad else bad}")


if __name__ == "__main__":
    main()
