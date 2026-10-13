# RootCause AI: end-to-end flow

```mermaid
flowchart TD
    subgraph Shop["Demo shop (5 FastAPI services, MySQL 8)"]
        GW[gateway] --> OR[orders] --> IN[inventory]
        OR --> PA[payments]
        OR --> NO[notifications]
        OR & IN & PA --> DB[(MySQL pool)]
    end
    LG[load generator] --> GW
    FI["faults/inject.py<br/>8 scenarios, TTL"] -. "POST /admin/fault" .-> Shop
    Shop -- "OpenTelemetry traces, metrics, logs" --> COL[OTel Collector, JSONL]
    COL --> ING[Ingestor] --> MG[(MongoDB)]
    MG --> FIND["find_incident<br/>window + clean baseline"]
    FIND --> A1["1 Correlate<br/>isolation forest + guards"]
    A1 --> A2["2 Dependency graph<br/>from traces"]
    A2 --> A3["3 Hypotheses<br/>try to falsify each"]
    A3 --> A4["4 Timeline<br/>every event has an evidence ref"]
    A4 --> A5["5 Conclude<br/>supported / inconclusive / no_incident"]
    A5 -. "optional narration, rejected unless citations are real" .-> LLM[Gemini]
    A5 --> REM["Remediation<br/>proposed fix, verified in sandbox copy"]
    A5 --> API[FastAPI] --> UI[React dashboard]
    REM --> API
    API --> MG
```

## Demo script (about 3 minutes)

1. `make up` and open http://localhost:8000. No Docker: `python -m rootcause.devstack up`, then `make serve`.
2. `python faults/inject.py run slow_notifications_trap --duration 90`. Orders looks slow, but notifications is the cause.
3. Click **Investigate latest incident** (or `python -m rootcause investigate`).
4. Show, in order: the diagnosis and confidence; the dependency graph with the root cause highlighted; the rejected `orders / latency` hypothesis and why; the timeline, where each row opens its raw log or span; the remediation card (`verified`, proposed, not applied).
5. `make eval-live` for the benchmark table.

## Interview talking points

- The LLM never decides: diagnosis is computed from telemetry, and prose is accepted only if every citation is real.
- It tries to falsify hypotheses, which is how it avoids blaming the busiest service.
- Three real-telemetry failures found and fixed (blips in the baseline, harness cooldown, single noisy bucket), each now a regression test.
- Why MySQL for the shop, MongoDB for telemetry, no graph DB.
