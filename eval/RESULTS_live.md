# Evaluation results (live)

> **Live mode**: the real five services (run by `python -m rootcause.devstack`, no Docker), real injected faults, real OpenTelemetry output. 60 s faults, 125 s healthy cooldown between runs.


16 fault runs, 2 healthy control runs.

| Method | Top-1 service | Top-1 service+fault | False-diagnosis rate | False-alarm rate (control) | Time per diagnosis |
|---|---|---|---|---|---|
| **RootCause AI** | 100% | 100% | 0% | 0% | 2.53 s median |
| Baseline: blame most-errors service | 50% | 12% | 86% | 0% | 0.01 s median |

Time per diagnosis is compute time once an incident window is known (as an alert would supply it). Detection latency is not reported: every window here starts at the fault, so it would read ~0 s by construction.

## Per scenario (strict accuracy: service and fault type)

| Scenario | Runs | RootCause AI | Most-errors baseline |
|---|---|---|---|
| latency_payments | 2 | 2/2 | 0/2 |
| crash_inventory | 2 | 2/2 | 0/2 |
| db_pool_orders | 2 | 2/2 | 0/2 |
| memory_leak_payments | 2 | 2/2 | 0/2 |
| bad_deploy_orders | 2 | 2/2 | 0/2 |
| error_burst_inventory | 2 | 2/2 | 2/2 |
| slow_notifications_trap | 2 | 2/2 | 0/2 |
| db_pool_payments | 2 | 2/2 | 0/2 |

_The naive-LLM baseline was skipped (set GEMINI_API_KEY to include it)._
