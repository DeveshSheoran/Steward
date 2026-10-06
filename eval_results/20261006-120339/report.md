# Steward evaluation report

LLM backend: `claude-code (subscription)` | runs: 2 | scenarios: 2

- **Task success (expected status AND ground truth OK): 2/2 = 100%**
- Ground truth OK: 2/2
- Expected final status reached: 2/2
- Verifier false-PASS (said PASS, ground truth wrong): 0/2
- Audit chain intact: 2/2; evidence hashes intact: 2/2
- Idempotent write retries performed by the gate: 3
- Median steps: 11.0; median tokens/run: 189715

| scenario | run | status | expected | ground truth | verifier | steps | tokens | s | notes |
|---|---|---|---|---|---|---|---|---|---|
| refund_5xx_retry | 1 | completed | completed | OK | PASS | 11 | 224148 | 155.2 |  |
| refund_applied_but_503 | 1 | completed | completed | OK | PASS | 11 | 155282 | 101.7 |  |
