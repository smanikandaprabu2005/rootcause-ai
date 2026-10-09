# Evaluation results (synthetic)

> **Synthetic mode**: simulated telemetry, a regression/ablation signal. Not the headline benchmark.


40 fault runs, 5 healthy control runs.

| Method | Top-1 service | Top-1 service+fault | False-diagnosis rate | False-alarm rate (control) | Time per diagnosis |
|---|---|---|---|---|---|
| **RootCause AI** | 100% | 100% | 0% | 0% | 1.57 s median |
| Baseline: blame most-errors service | 50% | 12% | 86% | 0% | 0.01 s median |

Time per diagnosis is compute time once an incident window is known (as an alert would supply it). Detection latency is not reported: every window here starts at the fault, so it would read ~0 s by construction.

## Per scenario (strict accuracy: service and fault type)

| Scenario | Runs | RootCause AI | Most-errors baseline |
|---|---|---|---|
| latency_payments | 5 | 5/5 | 0/5 |
| crash_inventory | 5 | 5/5 | 0/5 |
| db_pool_orders | 5 | 5/5 | 0/5 |
| memory_leak_payments | 5 | 5/5 | 0/5 |
| bad_deploy_orders | 5 | 5/5 | 0/5 |
| error_burst_inventory | 5 | 5/5 | 5/5 |
| slow_notifications_trap | 5 | 5/5 | 0/5 |
| db_pool_payments | 5 | 5/5 | 0/5 |

_The naive-LLM baseline was skipped (set GEMINI_API_KEY to include it)._
