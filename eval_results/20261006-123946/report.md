# Steward evaluation report

LLM backend: `claude-code (subscription)` | runs: 2 | scenarios: 2

- **Task success (expected status AND ground truth OK): 1/2 = 50%**
- Ground truth OK: 2/2
- Expected final status reached: 1/2
- Verifier false-PASS (said PASS, ground truth wrong): 0/1
- Audit chain intact: 2/2; evidence hashes intact: 2/2
- Idempotent write retries performed by the gate: 0
- Median steps: 11.0; median tokens/run: 232362

| scenario | run | status | expected | ground truth | verifier | steps | tokens | s | notes |
|---|---|---|---|---|---|---|---|---|---|
| onboard_finance_priv_approved | 1 | needs_human | completed | OK | INCONCLUSIVE | 18 | 398121 | 263.6 | Verification did not pass after 2 attempts: Confirmed: the account, the staff and finance groups, laptop request REQ-001 |
| weekly_report | 1 | completed | completed | OK | PASS | 4 | 66603 | 61.1 |  |
