# Steward evaluation report

LLM backend: `claude-code (subscription)` | runs: 1 | scenarios: 1

- **Task success (expected status AND ground truth OK): 1/1 = 100%**
- Ground truth OK: 1/1
- Expected final status reached: 1/1
- Verifier false-PASS (said PASS, ground truth wrong): 0/1
- Audit chain intact: 1/1; evidence hashes intact: 1/1
- Idempotent write retries performed by the gate: 0
- Median steps: 9; median tokens/run: 189066

| scenario | run | status | expected | ground truth | verifier | steps | tokens | s | notes |
|---|---|---|---|---|---|---|---|---|---|
| onboard_finance_priv_approved | 1 | completed | completed | OK | PASS | 9 | 189066 | 110.7 |  |
