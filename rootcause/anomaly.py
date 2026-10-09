"""Per-service anomaly detection: an isolation forest ranks multivariate deviation
from each service's own baseline; a magnitude guard keeps only actionable ones.

The forest answers "is this bucket unlike this service's healthy behaviour?".
The guard answers "is the change big enough to matter?" (otherwise a service
with a very quiet baseline would alarm on trivial noise). A bucket is anomalous
only if both agree, and every flagged feature is reported so the result is explainable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
from sklearn.ensemble import IsolationForest

from .features import FEATURES, bucketize
from .models import MetricPoint, Span

# (feature, noise floor for the robust scale, minimum absolute change that matters, direction)
_GUARDS = {
    "requests":       dict(floor=1.0,  min_delta=0.0,  mode="drop_ratio", ratio=0.5),
    "error_rate":     dict(floor=0.02, min_delta=0.05, mode="rise"),
    "p95_ms":         dict(floor=5.0,  min_delta=100.0, mode="rise", ratio=2.0),
    "self_p95_ms":    dict(floor=5.0,  min_delta=100.0, mode="rise", ratio=2.0),
    "rss_mb":         dict(floor=2.0,  min_delta=20.0, mode="rise"),
    "pool_in_use":    dict(floor=0.5,  min_delta=2.0,  mode="rise"),
    "inbound_fail":   dict(floor=0.02, min_delta=0.05, mode="rise"),
    "inbound_p95_ms": dict(floor=5.0,  min_delta=100.0, mode="rise", ratio=2.0),
}
_MIN_BASELINE_BUCKETS = 8


@dataclass
class ServiceAnomaly:
    service: str
    onset_ns: int
    anomalous_buckets: int
    total_buckets: int
    severity: float
    deviating: Dict[str, Dict[str, float]] = field(default_factory=dict)  # feature -> {baseline, worst}

    def to_dict(self) -> dict:
        return {
            "service": self.service, "onset_ns": self.onset_ns, "anomalous_buckets": self.anomalous_buckets,
            "total_buckets": self.total_buckets, "severity": round(self.severity, 4), "deviating": self.deviating,
        }


def _guard_flags(base: np.ndarray, row: np.ndarray) -> List[int]:
    flagged = []
    med = np.median(base, axis=0)
    mad = np.median(np.abs(base - med), axis=0) * 1.4826
    for j, feat in enumerate(FEATURES):
        g = _GUARDS[feat]
        scale = max(mad[j], g["floor"])
        z = (row[j] - med[j]) / scale
        if g["mode"] == "drop_ratio":
            if med[j] >= 3 and row[j] <= med[j] * g["ratio"] and z <= -4:
                flagged.append(j)
        else:
            delta = row[j] - med[j]
            if z >= 6 and delta >= g["min_delta"] and row[j] >= med[j] * g.get("ratio", 1.0):
                flagged.append(j)
    return flagged


def detect(spans: Sequence[Span], metrics: Sequence[MetricPoint], baseline: tuple, window: tuple,
           services: Sequence[str], bucket_ns: int, seed: int = 0) -> List[ServiceAnomaly]:
    """``baseline``/``window`` are (start_ns, end_ns). ``spans``/``metrics`` must cover both."""
    base_mats = bucketize(spans, metrics, baseline[0], baseline[1], bucket_ns, services)
    win_mats = bucketize(spans, metrics, window[0], window[1], bucket_ns, services)
    results: List[ServiceAnomaly] = []

    for svc in services:
        base, win = base_mats[svc], win_mats[svc]
        if base.shape[0] < _MIN_BASELINE_BUCKETS or not base[:, 0].any():
            continue  # no healthy history to compare against
        mu = np.median(base, axis=0)
        sd = np.maximum(np.median(np.abs(base - mu), axis=0) * 1.4826, [_GUARDS[f]["floor"] for f in FEATURES])
        scaled = (base - mu) / sd
        # A healthy baseline often has features that are exactly constant (error rate 0, pool use 0).
        # An isolation forest splits between a feature's min and max, so it can never isolate a value
        # that deviates along a constant axis. Fitting on noise-floor-sized jitter gives every feature
        # a range of about one noise floor, so a deviation of many floors is isolated in a few splits.
        rng = np.random.default_rng(seed)
        train = np.vstack([scaled + rng.normal(0.0, 0.5, scaled.shape) for _ in range(4)])
        forest = IsolationForest(n_estimators=200, contamination="auto", random_state=seed).fit(train)
        threshold = float(np.percentile(forest.decision_function(scaled), 2))
        scores = forest.decision_function((win - mu) / sd)

        bad: List[int] = []
        deviating: Dict[str, Dict[str, float]] = {}
        for b in range(win.shape[0]):
            if win[b, 0] == 0 and win[b, 6] == 0 and win[b, 4] == 0:
                continue  # no data at all for this bucket
            if scores[b] >= threshold:
                continue
            flags = _guard_flags(base, win[b])
            if not flags:
                continue
            bad.append(b)
            for j in flags:
                feat = FEATURES[j]
                worst = win[b, j] if feat != "requests" else min(win[b, j], deviating.get(feat, {}).get("worst", 1e18))
                prev = deviating.get(feat, {}).get("worst")
                if prev is not None and feat != "requests":
                    worst = max(prev, worst)
                deviating[feat] = {"baseline": round(float(mu[j]), 3), "worst": round(float(worst), 3)}
        if bad:
            results.append(ServiceAnomaly(
                service=svc, onset_ns=int(window[0] + bad[0] * bucket_ns), anomalous_buckets=len(bad),
                total_buckets=int(win.shape[0]), severity=float(-min(scores[b] for b in bad)), deviating=deviating,
            ))
    return sorted(results, key=lambda a: a.onset_ns)
