# RootCause AI: Autonomous Production Incident Investigator

When a service goes down, engineers correlate logs, traces and metrics by hand. RootCause AI does that investigation automatically and returns an **evidence-backed incident report**: every claim links to the raw log line, trace span or metric it came from, and every competing explanation is tested and either supported or ruled out.

**The LLM is optional and only narrates.** The diagnosis is computed from telemetry (reproducible and measurable). If an LLM summary is enabled, it is accepted only when every citation in it is a real evidence reference; otherwise it is discarded.

## How it works

```
 demo shop (5 FastAPI microservices + Postgres)  ──OpenTelemetry──>  collector ──> JSONL
                                                                                      │
   faults/inject.py  (8 failure scenarios + ground truth)                    ingest -> PostgreSQL
                                                                                      │
 ┌────────────────────────── LangGraph pipeline ───────────────────────────┐
 │ 1 Correlator   isolation forest on per-service metric/trace/log features │
 │ 2 Dependency   service graph from traces; failure-propagation reasoning  │
 │ 3 Hypotheses   one per (service, fault type); each is tested to FALSIFY  │
 │ 4 Timeline     evidence-referenced events, no LLM involved               │
 │ 5 Remediation  propose a fix, prove it in a disposable Docker sandbox    │
 └──────────────────────────────────────────────────────────────────────────┘
                                         │
                         FastAPI  ──>  React dashboard (timeline, graph, evidence, hypotheses)
```

- **Agent 1, correlation.** Turns spans, logs and metrics into per-service features (traffic, error rate, latency, self-time, memory, DB-pool use, how callers see the service). An isolation forest trained on each service's own healthy baseline flags deviations, and a magnitude guard keeps only changes that matter.
- **Agent 2, dependency graph.** Builds the call graph from traces, then separates root-cause candidates from victims. A service that merely receives less traffic is a victim, never a cause.
- **Agent 3, hypothesis testing.** For each anomalous service and each fault type (latency, crash, DB-pool exhaustion, memory leak, bad deploy, error source) it looks for evidence that *contradicts* the hypothesis. Example: "payments' own processing time is normal, so the slowness is propagated, not generated here." A decisive contradiction rejects the hypothesis outright; survivors are ranked by weighted evidence.
- **Remediation agent.** Maps the diagnosis to a playbook action, reproduces the fault in a throwaway second copy of the stack (`docker-compose.sandbox.yml`), applies the fix and measures whether health returns to baseline. It reports `verified`, `failed` or `unverified`, never touches production, and states "proposed, not applied".

The demo includes a deliberate trap, `slow_notifications_trap`: a slow, non-critical service makes *orders* look slow. Blaming the busiest or noisiest service gets it wrong; following the evidence gets it right.

## Quick start

Requires Docker (Compose v2).

```bash
make up                                  # shop + telemetry + RootCause AI, dashboard at http://localhost:8000
python faults/inject.py list             # the 8 scenarios
python faults/inject.py run latency_payments --duration 90
# then open http://localhost:8000 and click "Investigate latest incident"
# or: python -m rootcause investigate --last 120
make down
```

The dashboard also works without a backend: it opens on a bundled sample report in "demo data" mode.

Optional LLM narration: `export GEMINI_API_KEY=...` (default model `gemini-2.5-flash`). Sandboxed remediation needs Docker, so run `make serve` on the host rather than the containerised API.

## Evaluation

Known faults are injected and the diagnosis is scored against recorded ground truth:
**top-1 root-cause accuracy** (service, and service + fault type), **false-diagnosis rate** (wrong, among runs where it committed), **false-alarm rate** on healthy control runs, and **time per diagnosis**. Baselines: "blame the service with the most error logs", and "dump the logs into an LLM" (needs `GEMINI_API_KEY`).

**Results so far, on simulated telemetry** (`make eval-synthetic`: 8 scenarios × 5 seeded runs + 5 healthy controls; see [`eval/RESULTS_synthetic.md`](eval/RESULTS_synthetic.md)):

| Method | Top-1 service | Top-1 service + fault | False-diagnosis rate | False alarms (control) |
|---|---|---|---|---|
| **RootCause AI** | 100% | 100% | 0% | 0% |
| Blame most-errors service | 50% | 12% | 86% | 0% |

**Read this honestly.** The simulator produces telemetry in the same format and with the same failure signatures as the real demo stack, and it is the project author's model of how those failures look. The investigator's rules were developed against it, so 100% here is a **regression signal, not a benchmark**. The headline number comes from `make eval-live`, which injects the same faults into the real Docker stack and scores the result. That run needs a machine with Docker and about 1.5 hours; it has not been run yet, and results will be added to this README when it has. Expect real telemetry to be noisier than the simulation and the live accuracy to be lower.

## Tests

`python -m pytest tests -q`: 37 tests covering the fault layer, the detector (including a regression for the isolation-forest constant-feature blind spot), each of the 8 scenarios end to end, the trap case, evidence-reference integrity, the LLM citation guardrail, the API contract, and remediation honesty rules (`verified` / `failed` / `unverified`). PostgreSQL integration tests run when `ROOTCAUSE_TEST_PG` is set (CI does this).

## Limitations

- Live accuracy is unmeasured (see above); the full Docker stack has not been run end to end in the environment this was built in.
- Fault types are the six the demo can inject. An unfamiliar failure mode yields `inconclusive` rather than a guess, by design.
- The sandbox verifies the playbook action against the demo's fault-injection hooks; a real system would need its own reproduce-and-fix step.
- Single-fault incidents only. Concurrent unrelated faults are not handled.

## Layout

```
services/      demo microservices + shared telemetry / fault-injection module
faults/        chaos harness and scenario definitions
rootcause/     investigator: ingest, store, anomaly, graph, hypotheses, timeline, remediation, API, evaluation, simulator
dashboard/     React + TypeScript UI (Vite)
eval/          benchmark results
tests/         unit, integration and end-to-end tests
```
