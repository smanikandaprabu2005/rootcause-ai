# RootCause AI: Autonomous Production Incident Investigator

When a service goes down, engineers correlate logs, traces and metrics by hand. RootCause AI does that investigation automatically and returns an **evidence-backed incident report**: every claim links to the raw log line, trace span or metric it came from, and every competing explanation is tested and either supported or ruled out.

**The LLM is optional and only narrates.** The diagnosis is computed from telemetry (reproducible and measurable). If an LLM summary is enabled, it is accepted only when every citation in it is a real evidence reference; otherwise it is discarded.

## How it works

```
 demo shop (5 FastAPI microservices + MySQL 8)  ──OpenTelemetry──>  collector ──> JSONL
                                                                                      │
   faults/inject.py  (8 failure scenarios + ground truth)                    ingest -> MongoDB
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

## Data stores: why MySQL and MongoDB, and why no graph database

| Store | Used for | Why |
|---|---|---|
| **MySQL 8** | the demo shop (orders, inventory, payments) | A realistic transactional backend. Its bounded connection pool is what the `db_pool_exhaustion` fault exhausts, and the failure is observable (`PoolExhausted`, `db.pool.in_use` gauge). Every statement is validated against the MySQL dialect in tests. |
| **MongoDB** | RootCause's own telemetry (spans, logs, metrics) and saved reports | Telemetry is schemaless, append-heavy and queried by time range; reports are nested documents that are read whole. Indexed time-range queries fit a document store well. |
| *(no graph DB)* | service dependency graph | The graph has five nodes. It is derived per incident window from traces (parent/child span service pairs), so persisting it would only create a stale copy. A graph database becomes worthwhile at hundreds of services with long-lived topology queries; that is documented as a scaling step, not built speculatively. |

SQLite exists only as a zero-setup fallback (tests, `ROOTCAUSE_DB=sqlite:///...`). Both stores sit behind one `Store` protocol and pass the same contract tests.

## Quick start

Requires Docker (Compose v2).

```bash
make up                                  # shop + MySQL + MongoDB + collector + RootCause AI, dashboard at http://localhost:8000
python faults/inject.py list             # the 8 scenarios
python faults/inject.py run latency_payments --duration 90
# then open http://localhost:8000 and click "Investigate latest incident"
# or: python -m rootcause investigate            (finds the incident automatically)
make down
```

**No Docker?** `python -m rootcause.devstack up` runs the real five services as local processes with the same telemetry pipeline, Docker-style restart backoff and OOM emulation, so fault injection and the live benchmark work unchanged. Sandboxed remediation then uses `NativeSandbox` (a second copy of the services on other ports) instead of Docker Compose.

The dashboard also works without a backend: it opens on a bundled sample report in "demo data" mode.

Optional LLM narration: `export GEMINI_API_KEY=...` (default model `gemini-2.5-flash`).

**Incident discovery.** With no window given, the investigator scans the last ten minutes, finds the most recent sustained departure from normal, and uses the healthy stretch before it as the baseline. A fixed "last 90 s" window fails once an incident is already under way, because the fault ends up inside its own baseline.

**Faults never outlive their experiment.** Injected faults carry a TTL, stored as an absolute expiry that survives restarts, so a crash loop cannot leak into the next scenario.

## Evaluation

Known faults are injected and the diagnosis is scored against recorded ground truth:
**top-1 root-cause accuracy** (service, and service + fault type), **false-diagnosis rate** (wrong, among runs where it committed), **false-alarm rate** on healthy control runs, and **time per diagnosis**. Baselines: "blame the service with the most error logs", and "dump the logs into an LLM" (needs `GEMINI_API_KEY`; not run here).

**Live results** (real services and real OpenTelemetry output, faults injected over HTTP, scored against recorded ground truth; [`eval/RESULTS_live.md`](eval/RESULTS_live.md)): 8 scenarios x 2 runs + 2 healthy controls.

| Method | Top-1 service | Top-1 service + fault | False-diagnosis rate | False alarms (control) |
|---|---|---|---|---|
| **RootCause AI** | 16/16 | 16/16 | 0% | 0/2 |
| Blame most-errors service | 50% | 12% | 86% | 0% |

Median 2.5 s per diagnosis. **How much to trust this:** the sample is small (18 runs), the faults are the 8 the author wrote, and the detector was tuned during development on earlier live runs. The history, so the number is not a clean first shot:
1. First live pass: 7/8. The `bad_deploy` miss was real: the baseline contained its own error blips, which made the isolation-forest gate too strict. Fixed.
2. A benchmark with a 45 s cooldown scored 50%. That was a harness bug (the previous run's fault was inside the next run's 120 s baseline), not a detector result. The harness now rejects cooldowns shorter than the baseline.
3. The corrected run scored 16/16 but raised false alarms on both controls, caused by a single noisy bucket. Fixed by requiring an anomaly to last at least 2 buckets.
4. The run reported above was made fresh after that fix. All of these are regression tests now.

**Synthetic results** (`make eval-synthetic`, simulated telemetry, 8 scenarios x 5 runs + 5 controls; [`eval/RESULTS_synthetic.md`](eval/RESULTS_synthetic.md)): 100% top-1, 0% false diagnoses, 0% false alarms. The simulator is my model of how failures look and the rules were developed against it, so this is a **regression signal, not a benchmark**.

## Tests

`python -m pytest tests -q`: 83 tests. They cover the fault layer (including TTL), the detector (two regressions from real-data failures), each of the 8 scenarios end to end, the trap case, incident discovery, evidence-reference integrity, the LLM citation guardrail, the API contract, remediation honesty rules (`verified` / `failed` / `unverified`), a storage contract run on SQLite and MongoDB (mongomock locally, a real `mongo:7` in CI), the shop's MySQL SQL through the real PyMySQL pool path (real `mysql:8.4` in CI), and the Docker-free stack running real services. CI runs all of it with MongoDB and MySQL service containers.

## Limitations

- **Not run end to end:** `docker compose up` (CI only validates the compose files). Real MySQL 8.4 and MongoDB 7 are exercised by CI service containers and passed. The live numbers above come from the Docker-free stack with SQLite, not Compose.
- The live detector was tuned once after the first live pass (one missed scenario, fixed). The final run is a fresh set of injections, but the scenarios, thresholds and author are the same. It measures these 8 faults, not production incidents in general.
- Fault types are the six the demo can inject. An unfamiliar failure mode yields `inconclusive` rather than a guess, by design.
- The sandbox verifies the playbook action against the demo's fault-injection hooks; a real system would need its own reproduce-and-fix step.
- Single-fault incidents only. Concurrent unrelated faults are not handled.

See [docs/FLOW.md](docs/FLOW.md) for the full flow diagram and a 3-minute demo script.

## Layout



```
services/      demo microservices + shared telemetry / fault-injection module
faults/        chaos harness and scenario definitions
rootcause/     investigator: ingest, store, anomaly, graph, hypotheses, timeline, remediation, API, evaluation, simulator
dashboard/     React + TypeScript UI (Vite)
eval/          benchmark results
tests/         unit, integration and end-to-end tests
```
