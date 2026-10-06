# Steward evaluation report

LLM backend: `claude-code (subscription)` | runs: 1 | scenarios: 1

- **Task success (expected status AND ground truth OK): 0/1 = 0%**
- Ground truth OK: 0/1
- Expected final status reached: 0/1
- Verifier false-PASS (said PASS, ground truth wrong): 0/0
- Audit chain intact: 1/1; evidence hashes intact: 1/1
- Idempotent write retries performed by the gate: 0
- Median steps: 0; median tokens/run: 0

| scenario | run | status | expected | ground truth | verifier | steps | tokens | s | notes |
|---|---|---|---|---|---|---|---|---|---|
| weekly_report | 1 | dead | completed | FAIL | - | - | - | 14.2 | weekly_ops_report.md not written |
