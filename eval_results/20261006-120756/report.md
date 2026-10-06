# Steward evaluation report

LLM backend: `claude-code (subscription)` | runs: 2 | scenarios: 2

- **Task success (expected status AND ground truth OK): 2/2 = 100%**
- Ground truth OK: 2/2
- Expected final status reached: 2/2
- Verifier false-PASS (said PASS, ground truth wrong): 0/2
- Audit chain intact: 2/2; evidence hashes intact: 2/2
- Idempotent write retries performed by the gate: 0
- Median steps: 10.5; median tokens/run: 182935

| scenario | run | status | expected | ground truth | verifier | steps | tokens | s | notes |
|---|---|---|---|---|---|---|---|---|---|
| refund_ui_variant_b | 1 | completed | completed | OK | PASS | 13 | 247850 | 148.5 |  |
| onboard_ui_variant_b | 1 | completed | completed | OK | PASS | 8 | 118021 | 100.3 |  |
