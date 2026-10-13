"""Find the incident in the telemetry, instead of assuming where it is.

"Investigate the last 90 seconds against the 120 seconds before them" breaks the moment an incident is already
under way: the fault sits inside its own baseline, so a service that is slow *because of* the incident looks
normal ("p95 4010 ms, baseline 4009 ms") and every hypothesis gets falsified.

Discovery instead scans a look-back, flags the buckets where any service departs from its usual behaviour
(the same magnitude guards the detector uses), takes the most recent sustained run of such buckets as the
incident, and uses the healthy stretch *before* it as the baseline.

The reference for "usual" is the look-back's own median, recomputed once without the buckets already flagged, so an
incident that fills a large part of the look-back does not redefine normal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .anomaly import _GUARDS, _MIN_BASELINE_BUCKETS, _guard_flags
from .config import settings
from .features import FEATURES, bucketize
from .models import NS
from .store import Store


@dataclass
class Incident:
    found: bool
    window: Tuple[int, int]
    baseline: Tuple[int, int]
    ongoing: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {"found": self.found, "ongoing": self.ongoing, "note": self.note,
                "window_s": round((self.window[1] - self.window[0]) / NS, 1),
                "baseline_s": round((self.baseline[1] - self.baseline[0]) / NS, 1)}


def _flag_buckets(mats: dict, n: int, reference_mask: Optional[np.ndarray]) -> np.ndarray:
    bad = np.zeros(n, dtype=bool)
    for M in mats.values():
        ref = M if reference_mask is None or reference_mask.sum() < _MIN_BASELINE_BUCKETS else M[reference_mask]
        if not ref[:, 0].any():
            continue  # this service has no traffic to learn from
        for b in range(n):
            if M[b, 0] == 0 and M[b, 6] == 0 and M[b, 4] == 0:
                continue  # no data at all in this bucket
            if _guard_flags(ref, M[b]):
                bad[b] = True
    return bad


def _runs(bad: np.ndarray, max_gap: int = 1) -> List[Tuple[int, int]]:
    """Runs of True, merging runs separated by at most ``max_gap`` clean buckets. Inclusive indices."""
    runs: List[List[int]] = []
    for i, flag in enumerate(bad):
        if not flag:
            continue
        if runs and i - runs[-1][1] <= max_gap + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    return [(a, b) for a, b in runs]


def find_incident(store: Store, as_of_ns: Optional[int] = None, lookback_s: float = 600.0,
                  min_buckets: int = 2) -> Incident:
    bounds = store.time_bounds()
    if bounds is None:
        raise ValueError("no telemetry has been ingested yet")
    bucket_ns = int(settings.bucket_seconds * NS)
    hi = min(bounds[1], as_of_ns) if as_of_ns else bounds[1]
    lo = max(bounds[0], hi - int(lookback_s * NS))
    fallback_w = (max(bounds[0], hi - 90 * NS), hi)
    fallback_b = (max(bounds[0], fallback_w[0] - int(settings.baseline_seconds * NS)), fallback_w[0])

    spans, mets = store.spans(lo, hi), store.metrics(lo, hi)
    services = sorted({s.service for s in spans})
    n = int((hi - lo) // bucket_ns)
    if n < _MIN_BASELINE_BUCKETS + min_buckets or not services:
        return Incident(False, fallback_w, fallback_b, note="not enough telemetry to look for an incident")
    mats = bucketize(spans, mets, lo, hi, bucket_ns, services)

    bad = _flag_buckets(mats, n, None)
    if bad.any():  # second pass: learn "usual" from the buckets that were not flagged
        bad = _flag_buckets(mats, n, ~bad)
    runs = [r for r in _runs(bad) if r[1] - r[0] + 1 >= min_buckets]
    if not runs:
        return Incident(False, fallback_w, fallback_b, note="no service departs from its usual behaviour")

    first, last = runs[-1]
    ongoing = last >= n - 3
    start = lo + max(0, first - 1) * bucket_ns  # one bucket of margin so the onset is inside the window
    end = hi if ongoing else min(hi, lo + (last + 2) * bucket_ns + 10 * NS)
    baseline = (max(bounds[0], start - int(settings.baseline_seconds * NS)), start)
    note = ""
    if (baseline[1] - baseline[0]) < _MIN_BASELINE_BUCKETS * bucket_ns:
        after = hi - end
        if after >= _MIN_BASELINE_BUCKETS * bucket_ns:  # not enough history before: use the recovery afterwards
            baseline = (end, min(hi, end + int(settings.baseline_seconds * NS)))
            note = "little telemetry before the incident; baseline taken from after it"
        else:
            note = "little healthy telemetry around the incident; the baseline is thin"
    return Incident(True, (start, end), baseline, ongoing=ongoing, note=note)
