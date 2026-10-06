"""The AI-employee runtime: Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete.

Nothing here knows about invoices, refunds or any specific company. It only knows:
  * the Company Pack / memory (what the company is, its procedures, its lessons)
  * the tool registry and the policy Gate (what it may do)
  * how to loop, checkpoint, pause for humans, and verify.

Pause points (approval / question) end the run with a RunOutcome; the conversation is checkpointed in
`run_state`, so the worker can resume the *same* task later, possibly in a different process.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .audit import AuditLog, EvidenceStore
from .config import Config
from .gate import Gate
from .llm import LLM, TransientRunError
from .memory import CompanyPack, Memory
from .policy import Verdict
from .store import Store, now
from .tools.base import ErrorKind, Risk, Tool, ToolContext, ToolRegistry, ToolResult

# ----------------------------------------------------------------------------- control tools
CONTROL_TOOLS = [
    {"name": "note", "description": "Record an important fact you discovered (ids, amounts, emails). Persists across pauses.",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}},
    {"name": "ask_human", "description": "Ask the requester a question when required information is missing or ambiguous and cannot be found in the systems. The run pauses until they answer.",
     "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "escalate", "description": "Stop and hand the task to a human when you cannot complete it (policy denial, rejected approval, missing access, repeated failures). Explain what is done, what is not, and why.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "finish", "description": "Call when you believe the requested outcome is fully achieved. An independent verifier will then check the live systems.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string", "description": "What you did and the final state, with ids/amounts."}}, "required": ["summary"]}},
]

SPEC_TOOL = {
    "name": "submit_task_spec", "description": "Submit your understanding of the request.",
    "input_schema": {"type": "object", "properties": {
        "sop_id": {"type": "string", "description": "id of the best matching procedure, or empty string if none fits"},
        "intent": {"type": "string", "description": "One sentence: the outcome the requester actually wants."},
        "entities": {"type": "array", "items": {"type": "string"}, "description": "Concrete things named in the request as 'key=value' strings (names, ids, amounts)."},
        "success_criteria": {"type": "array", "items": {"type": "string"},
                             "description": "Checkable conditions in the company's systems that prove the outcome is achieved."},
        "plan": {"type": "array", "items": {"type": "string"}, "description": "Short ordered steps."},
        "blocking_questions": {"type": "array", "items": {"type": "string"},
                               "description": "Only questions whose answers are REQUIRED and cannot be found in the systems. Usually empty."},
        "risk_notes": {"type": "string"}},
        "required": ["sop_id", "intent", "success_criteria", "plan", "blocking_questions"]},
}

VERDICT_TOOL = {
    "name": "submit_verdict", "description": "Submit the independent verification result.",
    "input_schema": {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "INCONCLUSIVE"]},
        "criteria": {"type": "array", "items": {"type": "object", "properties": {
            "criterion": {"type": "string"}, "status": {"type": "string", "enum": ["met", "not_met", "unknown"]},
            "evidence": {"type": "string", "description": "What you directly observed in the system."}},
            "required": ["criterion", "status", "evidence"]}},
        "summary": {"type": "string"}},
        "required": ["verdict", "criteria", "summary"]},
}


def _tool_names_for_api(tools: list[Tool]) -> list[dict]:
    return [t.to_anthropic() for t in tools]


# ----------------------------------------------------------------------------- state
@dataclass
class RunState:
    task_id: str
    phase: str = "new"          # new | execute | paused_approval | awaiting_answer_spec | awaiting_answer_tool
    spec: dict = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)
    step: int = 0
    verify_rounds: int = 0
    nudges: int = 0
    pending_approvals: list[str] = field(default_factory=list)
    question: str | None = None
    partial_results: list[dict] = field(default_factory=list)   # tool_results already produced in a turn paused by ask_human
    await_answer_for: str | None = None
    skipped_ids: list[str] = field(default_factory=list)
    extra_answers: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0})
    last_verdict: dict | None = None


@dataclass
class RunOutcome:
    status: str                 # completed | awaiting_approval | waiting_input | needs_human | failed
    summary: str = ""
    result: dict = field(default_factory=dict)
    approval_ids: list[str] = field(default_factory=list)
    question: str | None = None


class Runtime:
    def __init__(self, *, cfg: Config, store: Store, audit: AuditLog, evidence: EvidenceStore, gate: Gate,
                 memory: Memory, registry: ToolRegistry, llm: LLM, env_factory=None, retry_sleep: float = 0.5,
                 max_verify_rounds: int = 2, company: str = "the company"):
        self.cfg, self.store, self.audit, self.evidence, self.gate = cfg, store, audit, evidence, gate
        self.memory, self.registry, self.llm = memory, registry, llm
        self.env_factory = env_factory or (lambda task_id, spec: {})
        self.retry_sleep, self.max_verify_rounds, self.company = retry_sleep, max_verify_rounds, company
        self.role = cfg.role

    # ------------------------------------------------------------------ public API
    def run(self, task_id: str) -> RunOutcome:
        task = dict(self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,)))
        st = self._load(task_id) or RunState(task_id)
        env: dict = {}
        try:
            if st.phase in ("awaiting_answer_spec", "awaiting_answer_tool"):
                return RunOutcome("waiting_input", question=st.question)
            if st.phase == "new":
                out = self._understand(task, st)
                if out:
                    return out
            elif st.phase == "paused_approval":
                if any((self.store.one("SELECT status FROM approvals WHERE id=?", (a,)) or {"status": ""})["status"] == "pending" for a in st.pending_approvals):
                    return RunOutcome("awaiting_approval", approval_ids=st.pending_approvals)
                self._resume_after_approval(st)
            env = self.env_factory(task_id, st.spec)
            return self._execute(task, st, env)
        finally:
            self._save(st)
            b = env.get("browser")
            if b is not None and hasattr(b, "close"):
                b.close()

    def provide_answer(self, task_id: str, answer: str) -> None:
        """Called (by API/CLI) when the human answers an ask_human question; the worker then re-queues the task."""
        st = self._load(task_id)
        if st is None or st.phase not in ("awaiting_answer_spec", "awaiting_answer_tool"):
            raise ValueError("task is not waiting for an answer")
        self.audit.append(task_id, "human.answer", "human", {"question": st.question, "answer": answer})
        if st.phase == "awaiting_answer_spec":
            st.extra_answers.append(f"Q: {st.question}\nA: {answer}")
            st.phase = "new"
        else:
            results = st.partial_results + [{"type": "tool_result", "tool_use_id": st.await_answer_for, "content": f"Human answered: {answer}"}]
            results += [{"type": "tool_result", "tool_use_id": i, "content": "skipped: run paused to ask the human", "is_error": True} for i in st.skipped_ids]
            st.messages.append({"role": "user", "content": results})
            st.partial_results, st.await_answer_for, st.skipped_ids = [], None, []
            st.phase = "execute"
        st.question = None
        self._save(st)

    # ------------------------------------------------------------------ persistence
    def _load(self, task_id: str) -> RunState | None:
        r = self.store.one("SELECT state FROM run_state WHERE task_id=?", (task_id,))
        return RunState(**json.loads(r["state"])) if r else None

    def _save(self, st: RunState) -> None:
        self.store.execute(
            "INSERT INTO run_state(task_id, state, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (st.task_id, json.dumps(asdict(st), default=str), now()))

    # ------------------------------------------------------------------ llm helper
    def _llm(self, st: RunState, *, model: str, system: str, messages, tools=None, tool_choice=None, label: str):
        resp = self.llm.complete(model=model, system=system, messages=messages, tools=tools, tool_choice=tool_choice, max_tokens=4096)
        st.usage["input_tokens"] += resp.usage.get("input_tokens", 0)
        st.usage["output_tokens"] += resp.usage.get("output_tokens", 0)
        st.usage["llm_calls"] += 1
        self.audit.append(st.task_id, "llm.call", "runtime", {"label": label, "model": model, **resp.usage, "stop": resp.stop_reason})
        for b in resp.blocks:
            if b["type"] == "text" and b["text"].strip():
                self.audit.append(st.task_id, "agent.message", label, {"text": b["text"][:1500]})
        return resp

    # ------------------------------------------------------------------ 1. understand
    def _understand(self, task: dict, st: RunState) -> RunOutcome | None:
        ctx = self.memory.context_for(task["goal"])
        for a in st.extra_answers:
            ctx.setdefault("clarifications", []).append(a)
        system = (f"You are the intake stage of Steward, an AI operator at {self.company}. Turn a short request into a precise task spec. "
                  "Use the candidate procedures (SOPs) and company context to fill in steps the request leaves unstated. "
                  "Pick the best-matching SOP id, or null if none fits. success_criteria must be checkable in the company's systems "
                  "(e.g. 'refund of INR 1500 recorded on order A-1001'), never about your own behaviour. "
                  "Only put something in blocking_questions if it is REQUIRED and cannot be discovered from the systems; otherwise leave it empty.")
        user = json.dumps({"request": task["goal"], "requested_by": task["requester"], "company_context": ctx}, indent=1, default=str)
        resp = self._llm(st, model=self.cfg.model, system=system, messages=[{"role": "user", "content": user}],
                         tools=[SPEC_TOOL], tool_choice={"type": "tool", "name": "submit_task_spec"}, label="understand")
        spec = next((b["input"] for b in resp.blocks if b["type"] == "tool_use"), None)
        if spec is None:
            raise TransientRunError("understand step returned no spec")
        sop = self.memory.pack.sops.get(spec.get("sop_id") or "")
        if sop:
            have = {c.lower() for c in spec["success_criteria"]}
            spec["success_criteria"] += [c for c in sop.done_when if c.lower() not in have]
            spec["systems"] = sop.systems
        spec["context"] = ctx
        st.spec = spec
        self.store.execute("UPDATE tasks SET spec=?, updated_at=? WHERE id=?", (json.dumps(spec, default=str), now(), task["id"]))
        self.audit.append(task["id"], "task.understood", "runtime", {k: spec.get(k) for k in ("sop_id", "intent", "entities", "success_criteria", "plan", "blocking_questions")})
        if spec.get("blocking_questions"):
            st.question = " ".join(spec["blocking_questions"])
            st.phase = "awaiting_answer_spec"
            self.audit.append(task["id"], "human.question", "runtime", {"question": st.question})
            return RunOutcome("waiting_input", question=st.question)
        intro = (f"Request from {task['requester']}: {task['goal']}\n\nIntent: {spec['intent']}\nSuccess criteria:\n"
                 + "\n".join(f"- {c}" for c in spec["success_criteria"]) + "\nPlan:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(spec["plan"]))
                 + "\n\nBegin.")
        if st.extra_answers:
            intro += "\n\nClarifications from the requester:\n" + "\n".join(st.extra_answers)
        st.messages = [{"role": "user", "content": intro}]
        st.phase = "execute"
        return None

    # ------------------------------------------------------------------ resume after approval
    def _resume_after_approval(self, st: RunState) -> None:
        notes = []
        for aid in st.pending_approvals:
            a = self.store.one("SELECT * FROM approvals WHERE id=?", (aid,))
            if a is None or a["status"] == "pending":
                continue
            if a["status"] == "rejected":
                notes.append(f"Approval {aid} for {a['tool']} was REJECTED by {a['decided_by']}: {a['comment'] or 'no comment'}. Do not retry that action; "
                             "tell the requester or pick a permitted alternative.")
                if a["comment"] and not self.store.one("SELECT 1 FROM lessons WHERE source=?", (f"approval:{aid}",)):
                    scope = f"sop:{st.spec['sop_id']}" if st.spec.get("sop_id") else "global"
                    self.memory.add_lesson(f"Reviewer {a['decided_by']} rejected '{a['tool']}' ({a['args'][:160]}): {a['comment']}",
                                           source=f"approval:{aid}", scope=scope, task_id=st.task_id)
            else:
                notes.append(f"Approval {aid} for {a['tool']} was GRANTED by {a['decided_by']}" + (f" ({a['comment']})" if a["comment"] else "") + ".")
        notes.append("The browser session was restarted while paused: re-open the relevant page and redo the form with EXACTLY the same values; "
                     "the approved action will then go through.")
        text = "RESUMED. " + " ".join(notes)
        self._append_user_text(st, text)
        st.pending_approvals, st.phase = [], "execute"
        self.audit.append(st.task_id, "task.resumed", "runtime", {"notes": notes})

    @staticmethod
    def _append_user_text(st: RunState, text: str) -> None:
        last = st.messages[-1] if st.messages else None
        if last and last["role"] == "user":
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            last["content"].append({"type": "text", "text": text})
        else:
            st.messages.append({"role": "user", "content": text})

    # ------------------------------------------------------------------ tools
    def _visible_tools(self, st: RunState, read_only: bool = False) -> list[Tool]:
        sop = self.memory.pack.sops.get(st.spec.get("sop_id") or "")
        tools = self.registry.select(sop.tools if sop and sop.tools else None)
        tools = [t for t in tools if self.gate.engine.tool_visible(self.role, t.name)]
        if read_only:
            tools = [t for t in tools if not callable(t.risk) and t.risk == Risk.R0_READ]
        return tools

    def _call_tool(self, tool: Tool, ctx: ToolContext, args: dict) -> ToolResult:
        attempts = 1 if tool.mutating else 3
        res = ToolResult(False, error="not run", error_kind=ErrorKind.PERMANENT)
        for i in range(1, attempts + 1):
            try:
                res = tool.handler(ctx, args)
            except (TimeoutError, ConnectionError) as e:
                res = ToolResult(False, error=f"{type(e).__name__}: {e}", error_kind=ErrorKind.TRANSIENT)
            except Exception as e:                                  # a tool bug must not kill the run
                res = ToolResult(False, error=f"{type(e).__name__}: {e}"[:400], error_kind=ErrorKind.PERMANENT)
            if res.ok or res.error_kind != ErrorKind.TRANSIENT or i == attempts:
                break
            self.audit.append(ctx.task_id, "tool.retry", "runtime", {"tool": tool.name, "attempt": i, "error": res.error})
            time.sleep(self.retry_sleep * 2 ** (i - 1))
        return res

    def _exec_block(self, st: RunState, block: dict, ctx: ToolContext, visible: dict[str, Tool], task_ctx: dict) -> tuple[dict, dict | None]:
        """Run one tool_use block. Returns (tool_result_block, pause) where pause is {'kind','approval_id'} or None."""
        tid, name, args = block["id"], block["name"], block.get("input") or {}

        def result(text: str, err: bool = False) -> dict:
            r = {"type": "tool_result", "tool_use_id": tid, "content": text}
            if err:
                r["is_error"] = True
            return r

        tool = visible.get(name)
        if tool is None:
            return result(f"Unknown or not-permitted tool '{name}'. Available: {', '.join(sorted(visible))}", True), None
        missing = [k for k in tool.input_schema.get("required", []) if k not in args]
        if missing:
            return result(f"Missing required argument(s): {', '.join(missing)}", True), None

        self.audit.append(st.task_id, "tool.call", "agent", {"tool": tool.name, "args": args})
        dec = self.gate.check(st.task_id, tool, args, role=self.role, task=task_ctx)
        if dec.verdict == Verdict.DENY:
            return result(dec.to_model_text(), True), None
        if dec.verdict == Verdict.REQUIRE_APPROVAL:
            return result(dec.to_model_text(), True), {"kind": "approval", "approval_id": dec.approval_id}

        res = self._call_tool(tool, ctx, args)
        self.audit.append(st.task_id, "tool.result", "tool", {
            "tool": tool.name, "ok": res.ok, "error": res.error, "error_kind": res.error_kind, "mutating": tool.mutating,
            "evidence": [e["id"] for e in res.evidence], "preview": (res.to_model_text(300))})
        if res.signal == "pause_for_approval":
            return result(res.error or "pending approval", True), {"kind": "approval", "approval_id": res.meta.get("approval_id")}
        return result(res.to_model_text(), not res.ok), None

    def _handle_control(self, st: RunState, block: dict) -> tuple[dict | None, str | None]:
        """Returns (tool_result or None if deferred, control) where control in {'finish','ask'}."""
        name, args, tid = block["name"], block.get("input") or {}, block["id"]
        if name == "note":
            self.memory.note(st.task_id, str(args.get("key")), str(args.get("value")))
            self.audit.append(st.task_id, "agent.note", "agent", {"key": args.get("key"), "value": args.get("value")})
            return {"type": "tool_result", "tool_use_id": tid, "content": "noted"}, None
        if name == "ask_human":
            return None, "ask"
        if name == "finish":
            return {"type": "tool_result", "tool_use_id": tid, "content": "received"}, "finish"
        if name == "escalate":
            return {"type": "tool_result", "tool_use_id": tid, "content": "escalated"}, "escalate"
        return None, None

    # ------------------------------------------------------------------ 2. execute
    def _system_prompt(self, st: RunState) -> str:
        spec, ctx = st.spec, st.spec.get("context", {})
        sops = "\n\n".join(f"### Procedure: {s['title']} ({s['id']})\n{s['body']}" for s in ctx.get("candidate_sops", [])
                           if s["id"] == spec.get("sop_id")) or "(no matching procedure; use judgement and keep actions minimal)"
        lessons = "\n".join(f"- {l['text']}" for l in ctx.get("lessons", [])) or "(none)"
        systems = json.dumps({k: {"description": v.get("description"), "url": v.get("base_url")} for k, v in ctx.get("systems", {}).items()}, indent=1)
        notes = json.dumps(self.memory.notes(st.task_id)) if self.memory.notes(st.task_id) else "(none yet)"
        return f"""You are Steward, an AI operator working inside {self.company}. You complete work requests end-to-end with the tools provided, on behalf of the requester. You are measured on whether the real outcome is achieved in the company's systems, not on what you say.

How you work
- Follow the company procedure below when it applies. The request is often short; the procedure and company context supply the steps and rules it leaves unstated.
- Before each action write ONE short sentence saying what you are about to do and why. Then act.
- Observe before you act and read every result carefully. Never assume an action worked; confirm it in the page/data.
- Company policy is enforced outside you. If an action is DENIED, do not look for a way around it: report it. If an action is PENDING APPROVAL the run pauses and resumes after a human decides.
- Never invent facts (ids, amounts, emails). If something required is missing and cannot be found in the systems, call ask_human.
- If you cannot complete the task (denied, rejected, no access), call escalate(reason) with what is done and what is not. Do not pretend success.
- Use note(key, value) for important facts you discover; they survive pauses.
- Do the minimum needed and change nothing the request does not require.
- When you believe the outcome is fully achieved, call finish(summary). An independent verifier will then check the live systems against the success criteria.

Systems you may use
{systems}

{sops}

Lessons learned from earlier feedback (apply them)
{lessons}

Your notes so far: {notes}
Success criteria: {json.dumps(spec.get('success_criteria', []))}"""

    def _execute(self, task: dict, st: RunState, env: dict) -> RunOutcome:
        ctx = ToolContext(st.task_id, self.store, self.audit, self.evidence, str(self.cfg.workdir), env)
        task_ctx = {"id": task["id"], "requester": task["requester"], "sop": st.spec.get("sop_id")}
        self.cfg.workdir.mkdir(parents=True, exist_ok=True)
        while st.step < self.cfg.max_steps:
            visible = {t.name.replace(".", "__"): t for t in self._visible_tools(st)}
            tools = _tool_names_for_api(list(visible.values())) + CONTROL_TOOLS
            resp = self._llm(st, model=self.cfg.model, system=self._system_prompt(st), messages=st.messages, tools=tools, label="agent")
            st.messages.append({"role": "assistant", "content": resp.blocks})
            uses = [b for b in resp.blocks if b["type"] == "tool_use"]
            st.step += 1
            if not uses:
                st.nudges += 1
                if st.nudges > 2:
                    return self._give_up(st, "The agent stopped acting without finishing or asking for help.")
                st.messages.append({"role": "user", "content": "Continue with tools, or call finish(summary) if the outcome is achieved, or ask_human if you are blocked."})
                self._save(st)
                continue
            st.nudges = 0

            results, pause, finish, ask = [], None, None, None
            for i, b in enumerate(uses):
                if pause or ask:
                    break
                r, control = self._handle_control(st, b) if b["name"] in {"note", "ask_human", "finish", "escalate"} else (None, None)
                if control == "ask":
                    ask = b
                    break
                if control == "escalate":
                    return self._give_up(st, b["input"].get("reason", "escalated by the agent"))
                if control == "finish":
                    finish = b
                    results.append(r)
                    continue
                if r is not None:
                    results.append(r)
                    continue
                r, pause = self._exec_block(st, b, ctx, visible, task_ctx)
                results.append(r)
            done_ids = {r["tool_use_id"] for r in results}
            remaining = [b["id"] for b in uses if b["id"] not in done_ids and not (ask and b["id"] == ask["id"])]

            if ask:
                st.question, st.phase = (ask["input"].get("question") or "?"), "awaiting_answer_tool"
                st.partial_results, st.await_answer_for, st.skipped_ids = results, ask["id"], [i for i in remaining]
                self.audit.append(st.task_id, "human.question", "agent", {"question": st.question})
                self._save(st)
                return RunOutcome("waiting_input", question=st.question)

            results += [{"type": "tool_result", "tool_use_id": i, "content": "skipped: run paused before this call", "is_error": True} for i in remaining]
            st.messages.append({"role": "user", "content": results})

            if pause:
                st.pending_approvals = list(dict.fromkeys(st.pending_approvals + [pause["approval_id"]]))
                st.phase = "paused_approval"
                self.audit.append(st.task_id, "task.paused", "runtime", {"reason": "approval", "approval_ids": st.pending_approvals})
                self._save(st)
                return RunOutcome("awaiting_approval", approval_ids=st.pending_approvals)

            if finish:
                verdict = self._verify(task, st, env, finish["input"].get("summary", ""))
                st.last_verdict = verdict
                if verdict["verdict"] == "PASS":
                    return self._complete(task, st, finish["input"].get("summary", ""), verdict)
                st.verify_rounds += 1
                if st.verify_rounds > self.max_verify_rounds or verdict["verdict"] == "INCONCLUSIVE" and st.verify_rounds >= 2:
                    return self._give_up(st, f"Verification did not pass after {st.verify_rounds} attempts: {verdict['summary']}", verdict)
                self._append_user_text(st, "VERIFICATION " + verdict["verdict"] + ": " + verdict["summary"] + "\n"
                                       + "\n".join(f"- [{c['status']}] {c['criterion']}: {c['evidence']}" for c in verdict["criteria"])
                                       + "\nFix what is not met (if permitted) and call finish again, or ask_human if blocked.")
            self._save(st)
        return self._give_up(st, f"Step budget of {self.cfg.max_steps} exhausted.")

    # ------------------------------------------------------------------ 3. verify (independent)
    def _verify(self, task: dict, st: RunState, env: dict, claimed: str) -> dict:
        ctx = ToolContext(st.task_id, self.store, self.audit, self.evidence, str(self.cfg.workdir), env)
        writes = [e["payload"] for e in self.audit.events(st.task_id, kinds=["net.write"])]
        mut = [e["payload"] for e in self.audit.events(st.task_id, kinds=["tool.result"]) if e["payload"].get("mutating") and e["payload"].get("ok")]
        system = ("You are the independent VERIFIER for Steward, an AI operator. Another agent claims it completed a task. Do NOT trust its claim. "
                  "Check each success criterion directly in the live systems using the read-only tools: open the relevant page or record and read what is actually there. "
                  "Never modify anything. If 'clarifications_from_requester' is present, the requester has since corrected or refined the original request: those answers take precedence "
                  "over the original wording, so judge any criterion that conflicts with a clarification by the clarified intent (and say so in the evidence). Facts you cannot observe in the business systems themselves (that a human approved something, who decided, what was written to the audit log) are in 'approvals_recorded_by_the_gate' and 'writes_recorded_by_the_gate': those records come from the permission layer, not from the agent, and you may rely on them for such facts (cite them as the evidence). A criterion is 'met' only if you directly observed it; if you could not check it, mark 'unknown'. "
                  "Verdict PASS only if every criterion is met. FAIL if any is not met. INCONCLUSIVE if you could not check enough. Finish by calling submit_verdict.")
        user = json.dumps({"request": task["goal"], "intent": st.spec.get("intent"), "success_criteria": st.spec.get("success_criteria"),
                           "clarifications_from_requester": [e["payload"] for e in self.audit.events(st.task_id, kinds=["human.answer"])],
                           "approvals_recorded_by_the_gate": [
                               {"tool": a["tool"], "args": json.loads(a["args"]), "approver_role": a["approver"], "status": a["status"],
                                "decided_by": a["decided_by"], "comment": a["comment"]}
                               for a in self.store.query("SELECT tool,args,approver,status,decided_by,comment FROM approvals WHERE task_id=? ORDER BY created_at", (st.task_id,))],
                           "agent_claim": claimed, "writes_recorded_by_the_gate": writes + mut,
                           "known_facts": self.memory.notes(st.task_id)}, indent=1, default=str)
        messages: list[dict] = [{"role": "user", "content": user}]
        visible = {t.name.replace(".", "__"): t for t in self._visible_tools(st, read_only=True)}
        tools = _tool_names_for_api(list(visible.values())) + [VERDICT_TOOL]
        task_ctx = {"id": task["id"], "requester": task["requester"], "sop": st.spec.get("sop_id"), "role": "verifier"}
        for _ in range(12):
            resp = self._llm(st, model=self.cfg.verifier_model, system=system, messages=messages, tools=tools, label="verifier")
            messages.append({"role": "assistant", "content": resp.blocks})
            uses = [b for b in resp.blocks if b["type"] == "tool_use"]
            v = next((b for b in uses if b["name"] == "submit_verdict"), None)
            if v:
                out = v["input"]
                self.audit.append(st.task_id, "verify.result", "verifier", out)
                return out
            if not uses:
                messages.append({"role": "user", "content": "Check the systems with the tools, then call submit_verdict."})
                continue
            results = []
            for b in uses:
                r, _p = self._exec_block(st, b, ctx, visible, task_ctx)
                results.append(r)
            messages.append({"role": "user", "content": results})
        out = {"verdict": "INCONCLUSIVE", "criteria": [], "summary": "Verifier ran out of steps."}
        self.audit.append(st.task_id, "verify.result", "verifier", out)
        return out

    # ------------------------------------------------------------------ 4. finish
    def _report(self, task: dict, st: RunState, summary: str, verdict: dict | None, status: str, note: str = "") -> dict:
        ev = self.evidence.list(task["id"])
        writes = [e["payload"] for e in self.audit.events(task["id"], kinds=["net.write"])]
        approvals = [dict(r) for r in self.store.query("SELECT id, tool, status, approver, decided_by, comment FROM approvals WHERE task_id=?", (task["id"],))]
        md = [f"# Task report: {task['goal']}", "", f"**Status:** {status}  ", f"**Requested by:** {task['requester']}  ", f"**Intent:** {st.spec.get('intent', '')}", "",
              "## Outcome", summary or note, ""]
        if note:
            md += ["## Note", note, ""]
        if verdict:
            md += [f"## Independent verification: {verdict['verdict']}", verdict["summary"], ""]
            md += [f"- **{c['status']}**: {c['criterion']} - {c['evidence']}" for c in verdict["criteria"]] + [""]
        if writes:
            md += ["## Changes made in company systems"] + [f"- {w['label']} ({w['path']}), HTTP {w['status']}, attempts={w['attempts']}" for w in writes] + [""]
        if approvals:
            md += ["## Approvals"] + [f"- {a['id']} {a['tool']}: {a['status']}" + (f" by {a['decided_by']}" if a["decided_by"] else "") + (f" - {a['comment']}" if a["comment"] else "") for a in approvals] + [""]
        if ev:
            md += ["## Evidence"] + [f"- {e['name']} (sha256 {e['sha256'][:12]})" for e in ev] + [""]
        md += [f"Steps: {st.step} | LLM calls: {st.usage['llm_calls']} | tokens in/out: {st.usage['input_tokens']}/{st.usage['output_tokens']}"]
        rec = self.evidence.save(task["id"], "report.md", "\n".join(md), kind="report", actor="runtime")
        return {"status": status, "summary": summary or note, "verdict": verdict, "writes": writes, "approvals": approvals,
                "evidence": [e["id"] for e in ev] + [rec["id"]], "report": rec["id"], "steps": st.step, "usage": st.usage}

    def _complete(self, task: dict, st: RunState, summary: str, verdict: dict) -> RunOutcome:
        st.phase = "done"
        result = self._report(task, st, summary, verdict, "completed")
        self.audit.append(task["id"], "task.completed", "runtime", {"summary": summary, "steps": st.step})
        return RunOutcome("completed", summary, result)

    def _give_up(self, st: RunState, reason: str, verdict: dict | None = None) -> RunOutcome:
        task = dict(self.store.one("SELECT * FROM tasks WHERE id=?", (st.task_id,)))
        st.phase = "done"
        result = self._report(task, st, "", verdict, "needs_human", note=reason)
        self.audit.append(st.task_id, "task.needs_human", "runtime", {"reason": reason})
        return RunOutcome("needs_human", reason, result)
