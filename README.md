# RootCause AI: Autonomous Production Incident Investigator

An agentic system that investigates production incidents the way an SRE would: correlate logs, traces and metrics, build the service dependency graph, test competing root-cause hypotheses against the evidence, and verify a fix in a disposable environment. The output is an **evidence-backed incident timeline**, not just an LLM-written explanation.

> **Status: Phase 1 of 7 complete** (demo system, telemetry, fault injection). The investigator agents come next.

## How it will be evaluated

Known faults are injected into a Docker-based microservice app, and the investigator is scored on:

- **Top-1 root-cause accuracy**
- **Time to diagnosis**
- **False-diagnosis rate**

...against a naive "dump the logs into an LLM" baseline. Ground truth for every run is recorded automatically in `eval/ground_truth.jsonl`.

## Architecture (Phase 1)

```
loadgen ──> gateway ──> orders ──> inventory ──> postgres
                           │──────> payments  ──> postgres
                           └──────> notifications   (best-effort, non-critical)

every service ──OTLP/HTTP──> otel-collector ──> data/telemetry/{traces,metrics,logs}.jsonl
```

Every service is a small FastAPI app built from one shared module (`services/common/base.py`) that provides:

- OpenTelemetry **traces, metrics and logs** over OTLP, plus JSON stdout logs carrying `trace_id`/`span_id`
- a **runtime fault-injection API** (`/admin/fault`) used by the chaos harness
- a bounded Postgres connection pool, so pool exhaustion is a real, observable failure
- custom metrics: `process.memory.rss`, `db.pool.in_use`

## Quick start

Requires Docker with Compose v2.

```bash
make up                         # build and start everything (postgres, collector, 5 services, load generator)
python faults/inject.py list    # show the 8 fault scenarios
python faults/inject.py run latency_payments --duration 90
tail -f data/telemetry/traces.jsonl
make down
```

Unit tests (no Docker needed): `pip install -r services/requirements.txt pytest httpx && make test`

## Fault scenarios

| Scenario | Root cause | What it tests |
|---|---|---|
| `latency_payments` | payments | slowness propagating up the call chain |
| `crash_inventory` | inventory | crash loop and restarts |
| `db_pool_orders` | orders | connection pool exhaustion (503s) |
| `memory_leak_payments` | payments | slow leak ending in an OOM kill |
| `bad_deploy_orders` | orders | errors that start right after a deploy event |
| `error_burst_inventory` | inventory | intermittent errors propagating upstream |
| `slow_notifications_trap` | notifications | **trap:** orders looks slow, but the cause is a non-critical dependency |
| `db_pool_payments` | payments | a failure that surfaces as errors in orders |

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1 | Demo microservices, OpenTelemetry, fault injector | Done |
| 2 | Telemetry ingestion and query API (FastAPI + PostgreSQL), isolation-forest metric anomaly detector | Next |
| 3 | Agent 1 (log/trace/metric correlation), Agent 2 (dependency graph and failure propagation) | |
| 4 | Agent 3 (hypothesis generation and falsification), evidence-backed timeline | |
| 5 | Remediation agent with sandboxed fix verification | |
| 6 | React dashboard | |
| 7 | Evaluation harness, baseline comparison, results, demo video | |

## Project layout

```
services/        FastAPI microservices + shared telemetry/fault module
faults/          chaos harness and scenario definitions
loadgen/         steady background traffic
otel/            OpenTelemetry Collector config
db/              Postgres schema and seed data
eval/            ground-truth records (generated)
tests/           unit tests for the fault layer
```
