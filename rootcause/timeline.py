"""Evidence-backed incident timeline: every entry is a real log line, metric or
detector finding with a reference back to the raw telemetry. No LLM involved."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Sequence, Set, Tuple

from .anomaly import ServiceAnomaly
from .hypotheses import Context, _log_ref
from .models import NS

_NOTABLE = {"deploy", "service_started", "crash", "db_pool_exhausted", "request_error", "upstream_error", "notify_skipped"}
_LABEL = {
    "deploy": "Deployment", "service_started": "Service restart", "crash": "Fatal error",
    "db_pool_exhausted": "DB pool exhausted", "request_error": "Request errors",
    "upstream_error": "Failed downstream calls", "notify_skipped": "Degraded (best-effort call failed)",
}


def build_timeline(ctx: Context, anomalies: Sequence[ServiceAnomaly], on_path: Set[str]) -> List[Dict[str, Any]]:
    t_zero = ctx.window[0]
    entries: List[Dict[str, Any]] = []

    def add(ts: int, svc: str, kind: str, summary: str, ref: str, count: int = 1) -> None:
        entries.append({
            "ts_ns": ts, "t_rel_s": round((ts - t_zero) / NS, 1), "service": svc, "kind": kind,
            "summary": summary + (f" (x{count})" if count > 1 else ""), "ref": ref, "on_path": svc in on_path,
        })

    groups: Dict[Tuple[str, str, str], List] = defaultdict(list)
    for r in ctx.w_logs:
        event = r.event or ("fatal" if r.severity == "CRITICAL" else None)
        if event in _NOTABLE:
            groups[(r.service, event, str(r.attrs.get("upstream", "")))].append(r)
    for (svc, event, upstream), recs in groups.items():
        first = recs[0]
        label = _LABEL.get(event, event)
        detail = first.body if len(first.body) <= 110 else first.body[:107] + "..."
        if event == "upstream_error":
            label = f"Failed calls to {upstream}"
        add(first.ts_ns, svc, "log", f"{label}: {detail}", _log_ref(first), count=len(recs))

    for a in anomalies:
        feats = ", ".join(a.deviating)
        add(a.onset_ns, a.service, "anomaly", f"Isolation forest flags {a.service} ({feats})", f"anomaly:{a.service}",
            count=1)

    for svc in ctx.services:
        sat = next((m for m in ctx.series(svc, "db.pool.in_use") if m.value >= 4), None)
        if sat:
            add(sat.ts_ns, svc, "metric", f"DB pool saturated ({sat.value:.0f}/5 connections in use)",
                f"metric:db.pool.in_use@{svc}")
    entries.sort(key=lambda e: e["ts_ns"])
    return entries
