# Steward evaluation report

LLM backend: `claude-code (subscription)` | runs: 2 | scenarios: 2

- **Task success (expected status AND ground truth OK): 1/2 = 50%**
- Ground truth OK: 2/2
- Expected final status reached: 1/2
- Verifier false-PASS (said PASS, ground truth wrong): 0/1
- Audit chain intact: 2/2; evidence hashes intact: 2/2
- Idempotent write retries performed by the gate: 0
- Median steps: 9; median tokens/run: 161246

| scenario | run | status | expected | ground truth | verifier | steps | tokens | s | notes |
|---|---|---|---|---|---|---|---|---|---|
| onboard_eng | 1 | completed | completed | OK | PASS | 9 | 161246 | 93.9 |  |
| onboard_finance_priv_approved | 1 | dead | completed | OK | - | - | - | 94.2 | claude CLI failed (exit 1): You've hit your session limit · resets 3:20pm (Asia/Kolkata) |
