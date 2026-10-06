# Steward: architecture (living doc)

Steward is an **AI-employee runtime**. The agent loop, tools, permission layer and queue are generic.
Everything company-specific (SOPs, policies, systems, people) lives in a **Company Pack**.
A new workflow = new pack entries (+ a mock app for the demo), not new core code.

```
Request -> Context Resolver -> Task Spec -> Plan/Execute loop -> Verifier -> Evidence pack
              (company memory)              (every tool call: Policy Gate -> Tool -> Audit)
Queue (SQLite) -> Worker -> retries / backoff / pause for approval / resume
```

## Components
| Module | Role | Status |
|---|---|---|
| `store.py` | SQLite (WAL): tasks/queue, events, approvals, evidence | done |
| `audit.py` | hash-chained audit log, evidence store with sha256 | done |
| `tools/base.py` | Tool, Risk R0-R3, ToolResult, error kinds, registry (least-privilege view) | done |
| `policy.py` | deterministic policy engine, sandboxed rule expressions, fail-closed | done |
| `gate.py` | choke point: policy -> scoped single-use approval grants -> audit | done |
| `memory.py` | Company Pack loader, BM25 retrieval, learned lessons, working notes | done |
| `tools/browser.py` | Playwright tools + network-level mutation gate, idempotent retries | done |
| `mock_apps/` | Northwind Admin Console / Helpdesk / Billing with chaos toggles (outside core) | done |
| `llm.py` | LLM interface: AnthropicLLM, ClaudeCodeLLM (own plan, dev only), OpenAI-compatible (Gemini/Groq/...), ScriptedLLM test double | done |
| `runtime.py` | understand -> execute -> verify loop, checkpoints, approval/question pause+resume, escalate | done |
| `queue.py` / `worker.py` | leases, heartbeat, backoff+jitter, dead-letter, auto-resume after human decision | done |
| `tools/standard.py` | files, read-only http, outbox (mock mail), directory | done |
| `app.py` / `cli.py` | composition root; `python -m steward run/submit/worker/approve/audit/...` | done |
| company_pack/ | policies, systems, directory, 3 SOPs (refund, onboarding, weekly report) | done |
| `evals.py` | ground-truth evaluation: chaos scenarios, oracles on real system state, verifier false-PASS rate | done |
| `dashboard/` | timeline, approvals inbox, evidence viewer | not built (CLI only) |

## Key decisions
1. **Policy is outside the LLM.** The model proposes; `Gate.check` decides. Rules see tool + *args*.
2. **Approvals are scoped grants**: bound to (task, tool, exact args), expire, single-use.
   Approving "refund 2500" cannot be replayed as "refund 9000".
3. **Fail closed**: a deny/approval rule whose expression errors counts as matched.
4. **Deny > require_approval > allow-rule > default-by-risk.** Role allow/deny-lists run first.
5. **Tamper-evident audit**: each event hash covers the previous one. Known limit: tail truncation
   needs an external anchor (e.g. periodically publishing the head hash).
6. **Browser safety at the network layer**: every non-GET request is intercepted, classified with
   `systems.yaml`, and run through the gate *before* it is sent. Allowed writes carry a deterministic
   Idempotency-Key and are retried on 5xx/timeouts, so an ambiguous failure never double-applies.
   Off-site hosts are blocked outright. (Tested against real Chromium: tests/test_browser_gate.py.)
7. **Verification reads the system of record**, not the agent's own claim: a separate verifier pass gets only
   read-only tools, the success criteria and the gate's record of writes. FAIL sends the agent back to fix;
   repeated FAIL/INCONCLUSIVE escalates instead of claiming success.
8. **Pause/resume is a checkpoint, not a blocked thread**: the conversation is stored in `run_state`; an approval
   or question ends the run segment, and any worker can resume it. Reviewer rejection comments become scoped
   lessons that are injected into future runs of the same procedure.

## Known limitations
See the README for the full list; additionally:
- After an approval pause the browser form state is lost; the agent re-fills the page on resume.
- Policy expressions are a small sandboxed subset of Python, not a full policy language (no OPA/Cedar).
- Agent quality was developed against a scripted LLM; real-model behaviour varies by backend (the `claude_code` and free-tier backends are slower and less reliable at tool-calling than the API).
- Audit-log tail truncation is undetectable without an external anchor.
