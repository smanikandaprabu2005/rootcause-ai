"""Turn raw spans/metrics into per-service, per-time-bucket feature vectors."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Sequence

import numpy as np

from .models import MetricPoint, Span

FEATURES = [
    "requests",         # server spans handled in the bucket
    "error_rate",       # fraction of those that failed (5xx / error status)
    "p95_ms",           # server-side latency
    "self_p95_ms",      # latency excluding time spent waiting on downstream calls
    "rss_mb",           # resident memory
    "pool_in_use",      # DB connections checked out
    "inbound_fail",     # fraction of *callers'* requests to this service that failed
    "inbound_p95_ms",   # latency as seen by callers
]


def percentile(values: Sequence[float], q: float) -> float:
    return float(np.percentile(values, q)) if len(values) else 0.0


def self_times_ms(spans: Sequence[Span]) -> Dict[str, float]:
    """Server-span duration minus time spent in its downstream (client) calls."""
    child_ms: Dict[str, float] = defaultdict(float)
    for s in spans:
        if s.kind == "client" and s.parent_id:
            child_ms[s.parent_id] += s.duration_ms
    return {s.span_id: max(0.0, s.duration_ms - child_ms.get(s.span_id, 0.0)) for s in spans if s.kind == "server"}


def bucketize(spans: Sequence[Span], metrics: Sequence[MetricPoint], start_ns: int, end_ns: int,
              bucket_ns: int, services: Sequence[str]) -> Dict[str, np.ndarray]:
    """Returns {service: array of shape (n_buckets, len(FEATURES))}."""
    n = max(1, int((end_ns - start_ns) // bucket_ns))
    idx = lambda ts: int((ts - start_ns) // bucket_ns)  # noqa: E731
    self_ms = self_times_ms(spans)

    srv: Dict[str, List[List[Span]]] = {s: [[] for _ in range(n)] for s in services}
    inb: Dict[str, List[List[Span]]] = {s: [[] for _ in range(n)] for s in services}
    for sp in spans:
        b = idx(sp.start_ns)
        if not 0 <= b < n:
            continue
        if sp.kind == "server" and sp.service in srv:
            srv[sp.service][b].append(sp)
        elif sp.kind == "client" and sp.peer in inb:
            inb[sp.peer][b].append(sp)

    rss: Dict[str, List[List[float]]] = {s: [[] for _ in range(n)] for s in services}
    pool: Dict[str, List[List[float]]] = {s: [[] for _ in range(n)] for s in services}
    for m in metrics:
        b = idx(m.ts_ns)
        if 0 <= b < n and m.service in rss:
            if m.name == "process.memory.rss":
                rss[m.service][b].append(m.value / 1024 / 1024)
            elif m.name == "db.pool.in_use":
                pool[m.service][b].append(m.value)

    out: Dict[str, np.ndarray] = {}
    for svc in services:
        mat = np.zeros((n, len(FEATURES)))
        for b in range(n):
            s_list, i_list = srv[svc][b], inb[svc][b]
            mat[b, 0] = len(s_list)
            if s_list:
                mat[b, 1] = sum(1 for s in s_list if s.is_error) / len(s_list)
                mat[b, 2] = percentile([s.duration_ms for s in s_list], 95)
                mat[b, 3] = percentile([self_ms[s.span_id] for s in s_list], 95)
            mat[b, 4] = max(rss[svc][b]) if rss[svc][b] else 0.0
            mat[b, 5] = max(pool[svc][b]) if pool[svc][b] else 0.0
            if i_list:
                mat[b, 6] = sum(1 for s in i_list if s.is_error) / len(i_list)
                mat[b, 7] = percentile([s.duration_ms for s in i_list], 95)
        out[svc] = mat
    return out
