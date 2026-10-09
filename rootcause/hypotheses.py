"""Hypothesis generation and falsification.

For every anomalous service and every fault type we form a hypothesis and then
*try to knock it down* against the collected telemetry. Each test returns
evidence that supports or contradicts the hypothesis, pointing at the raw log
line, span or metric it came from. A contradiction marked ``decisive`` rejects
the hypothesis outright (e.g. "payments has no database, so it cannot have
exhausted its connection pool"). Survivors are ranked by weighted evidence.

Nothing here calls an LLM: the diagnosis is a function of the telemetry, which
is what makes it reproducible and measurable.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .features import percentile, self_times_ms
from .graph import Edge, callees
from .models import NS, Evidence, Hypothesis, LogRec, MetricPoint, Span
from .store import Store

FAULT_TYPES = ["latency", "crash", "db_pool_exhaustion", "memory_leak", "bad_deploy", "error"]
_CONN_ERR = ("connection refused", "remotedisconnected", "connection aborted", "max retries exceeded")


def _ev(kind, svc, summary, supports=True, weight=1.0, ts=None, ref=None, decisive=False) -> Evidence:
    return Evidence(kind=kind, service=svc, summary=summary, ts_ns=ts, ref=ref, supports=supports,
                    weight=weight, decisive=decisive)


def _log_ref(r: LogRec) -> str:
    return f"log:{r.id}" if r.id is not None else f"log:{r.service}@{r.ts_ns}"


def _span_ref(s: Span) -> str:
    return f"trace:{s.trace_id}/span:{s.span_id}"


class Context:
    """Everything the tests need, loaded once for a (baseline, window) pair."""

    def __init__(self, store: Store, window: Tuple[int, int], baseline: Tuple[int, int]):
        self.window, self.baseline = window, baseline
        lo, hi = min(baseline[0], window[0]), max(baseline[1], window[1])
        spans, logs, mets = store.spans(lo, hi), store.logs(lo, hi), store.metrics(lo, hi)
        self.all_spans = spans
        self.self_ms = self_times_ms(spans)
        self.services = sorted({s.service for s in spans})
        self.w_spans = [s for s in spans if window[0] <= s.start_ns < window[1]]
        self.b_spans = [s for s in spans if baseline[0] <= s.start_ns < baseline[1]]
        self.w_logs = [r for r in logs if window[0] <= r.ts_ns < window[1]]
        self.b_logs = [r for r in logs if baseline[0] <= r.ts_ns < baseline[1]]
        self.w_mets = [m for m in mets if window[0] <= m.ts_ns < window[1]]
        self.b_mets = [m for m in mets if baseline[0] <= m.ts_ns < baseline[1]]

    # ---- spans --------------------------------------------------------------
    def server(self, svc: str, period: str = "w") -> List[Span]:
        src = self.w_spans if period == "w" else self.b_spans
        return [s for s in src if s.service == svc and s.kind == "server"]

    def err_rate(self, svc: str, period: str = "w") -> float:
        sp = self.server(svc, period)
        return sum(1 for s in sp if s.is_error) / len(sp) if sp else 0.0

    def self_p95(self, svc: str, period: str = "w") -> float:
        return percentile([self.self_ms[s.span_id] for s in self.server(svc, period)], 95)

    def inbound(self, svc: str, period: str = "w") -> List[Span]:
        src = self.w_spans if period == "w" else self.b_spans
        return [s for s in src if s.kind == "client" and s.peer == svc]

    def slowest_self(self, svc: str) -> Optional[Span]:
        sp = self.server(svc)
        return max(sp, key=lambda s: self.self_ms[s.span_id]) if sp else None

    # ---- logs ---------------------------------------------------------------
    def events(self, svc: str, event: str, period: str = "w") -> List[LogRec]:
        src = self.w_logs if period == "w" else self.b_logs
        return [r for r in src if r.service == svc and r.event == event]

    def inbound_errors(self, svc: str) -> List[LogRec]:
        return [r for r in self.w_logs if r.event == "upstream_error" and r.attrs.get("upstream") == svc]

    # ---- metrics ------------------------------------------------------------
    def series(self, svc: str, name: str, period: str = "w") -> List[MetricPoint]:
        src = self.w_mets if period == "w" else self.b_mets
        return [m for m in src if m.service == svc and m.name == name]

    def has_db(self, svc: str) -> bool:
        return bool(self.series(svc, "db.pool.in_use", "w") or self.series(svc, "db.pool.in_use", "b"))


# --------------------------------------------------------------------------- #
# One falsification routine per fault type
# --------------------------------------------------------------------------- #
def _largest_dependency(ctx: Context, svc: str) -> Optional[Tuple[str, float, float]]:
    """The callee that accounts for the most time inside ``svc``'s requests, vs baseline."""
    def mean_ms(spans: Sequence[Span], server_ids: set) -> Dict[str, float]:
        acc: Dict[str, List[float]] = {}
        for s in spans:
            if s.kind == "client" and s.parent_id in server_ids and s.peer:
                acc.setdefault(s.peer, []).append(s.duration_ms)
        return {k: float(np.mean(v)) for k, v in acc.items()}

    w = mean_ms(ctx.w_spans, {s.span_id for s in ctx.server(svc, "w")})
    b = mean_ms(ctx.b_spans, {s.span_id for s in ctx.server(svc, "b")})
    if not w:
        return None
    callee = max(w, key=w.get)
    return callee, b.get(callee, 0.0), w[callee]


def test_latency(ctx: Context, svc: str) -> List[Evidence]:
    out: List[Evidence] = []
    w, b = ctx.self_p95(svc, "w"), max(ctx.self_p95(svc, "b"), 5.0)
    if w >= max(3 * b, b + 100):
        slow = ctx.slowest_self(svc)
        out.append(_ev("span", svc, f"{svc} spends {w:.0f} ms p95 inside itself (baseline {b:.0f} ms), "
                       "excluding time waiting on downstream calls", weight=3,
                       ts=slow.start_ns if slow else None, ref=_span_ref(slow) if slow else None))
    else:
        dep = _largest_dependency(ctx, svc)
        why = (f"; its time is spent waiting on {dep[0]} (mean {dep[2]:.0f} ms vs baseline {dep[1]:.0f} ms)"
               if dep and dep[2] > max(3 * dep[1], dep[1] + 100) else "")
        out.append(_ev("stat", svc, f"{svc}'s own processing time is normal ({w:.0f} ms p95, baseline {b:.0f} ms){why}",
                       supports=False, weight=3, decisive=True, ref=f"stat:{svc}.self_p95"))
    inb = percentile([s.duration_ms for s in ctx.inbound(svc, "w")], 95)
    inb_b = max(percentile([s.duration_ms for s in ctx.inbound(svc, "b")], 95), 5.0)
    if inb >= max(3 * inb_b, inb_b + 100):
        out.append(_ev("stat", svc, f"callers see {svc} responses at {inb:.0f} ms p95 (baseline {inb_b:.0f} ms)",
                       weight=1, ref=f"stat:{svc}.inbound_p95"))
    pool = ctx.events(svc, "db_pool_exhausted")
    if pool:
        out.append(_ev("log", svc, f"{svc} is waiting on an exhausted DB pool, which explains the delay without a "
                       "separate latency fault", supports=False, weight=2, ts=pool[0].ts_ns, ref=_log_ref(pool[0])))
    return out


def test_db_pool(ctx: Context, svc: str) -> List[Evidence]:
    if not ctx.has_db(svc):
        return [_ev("stat", svc, f"{svc} has no database connection pool", supports=False, weight=5, decisive=True,
                    ref=f"stat:{svc}.no_db")]
    out: List[Evidence] = []
    logs = ctx.events(svc, "db_pool_exhausted")
    if logs:
        out.append(_ev("log", svc, f"{len(logs)} 'connection pool exhausted' errors in {svc}: \"{logs[0].body[:90]}\"",
                       weight=4, ts=logs[0].ts_ns, ref=_log_ref(logs[0])))
    else:
        out.append(_ev("stat", svc, f"no pool-exhaustion errors logged by {svc}", supports=False, weight=4,
                       decisive=True, ref=f"stat:{svc}.pool_logs"))
    pool = ctx.series(svc, "db.pool.in_use")
    if pool:
        sat = sum(1 for m in pool if m.value >= 4) / len(pool)
        if sat >= 0.4:
            out.append(_ev("metric", svc, f"{svc} pool in use at >=4 of 5 connections in {sat:.0%} of samples",
                           weight=3, ts=pool[0].ts_ns, ref=f"metric:db.pool.in_use@{svc}"))
        elif max(m.value for m in pool) < 3:
            out.append(_ev("metric", svc, f"{svc} pool never above {max(m.value for m in pool):.0f} connections",
                           supports=False, weight=2, ref=f"metric:db.pool.in_use@{svc}"))
    return out


def test_memory_leak(ctx: Context, svc: str) -> List[Evidence]:
    pts = ctx.series(svc, "process.memory.rss")
    base = [m.value / 1048576 for m in ctx.series(svc, "process.memory.rss", "b")]
    out: List[Evidence] = []
    if not pts:
        # a service that is OOM-killed on every cycle may leave few samples; fall through to restart signature
        pass
    else:
        mb = [m.value / 1048576 for m in pts]
        base_med = float(np.median(base)) if base else min(mb)
        if max(mb) >= base_med + 40:
            out.append(_ev("metric", svc, f"{svc} RSS climbed from {base_med:.0f} MB (baseline) to {max(mb):.0f} MB",
                           weight=4, ts=pts[int(np.argmax(mb))].ts_ns, ref=f"metric:process.memory.rss@{svc}"))
        else:
            out.append(_ev("metric", svc, f"{svc} RSS stayed flat (peak {max(mb):.0f} MB vs baseline {base_med:.0f} MB)",
                           supports=False, weight=4, decisive=True, ref=f"metric:process.memory.rss@{svc}"))
    restarts = ctx.events(svc, "service_started")
    fatal = [r for r in ctx.w_logs if r.service == svc and (r.event == "crash" or r.severity == "CRITICAL")]
    if restarts and not fatal:
        out.append(_ev("log", svc, f"{svc} restarted {len(restarts)}x with no fatal log: the signature of an "
                       "external kill such as the OOM killer", weight=2, ts=restarts[0].ts_ns, ref=_log_ref(restarts[0])))
    return out


def test_crash(ctx: Context, svc: str) -> List[Evidence]:
    out: List[Evidence] = []
    fatal = [r for r in ctx.w_logs if r.service == svc and (r.event == "crash" or r.severity == "CRITICAL")]
    restarts = ctx.events(svc, "service_started")
    conn = [r for r in ctx.inbound_errors(svc) if any(t in r.body.lower() for t in _CONN_ERR)]
    if fatal:
        out.append(_ev("log", svc, f"{len(fatal)} fatal log(s) from {svc}: \"{fatal[0].body[:80]}\"", weight=4,
                       ts=fatal[0].ts_ns, ref=_log_ref(fatal[0])))
    if restarts:
        out.append(_ev("log", svc, f"{svc} restarted {len(restarts)}x during the window", weight=2,
                       ts=restarts[0].ts_ns, ref=_log_ref(restarts[0])))
    if conn:
        out.append(_ev("log", svc, f"callers hit {len(conn)} connection errors reaching {svc}", weight=2,
                       ts=conn[0].ts_ns, ref=_log_ref(conn[0])))
    if restarts and not fatal:
        out.append(_ev("stat", svc, f"{svc} restarted without logging a fatal error, so this is not an application crash",
                       supports=False, weight=3, ref=f"stat:{svc}.restarts_no_fatal"))
    if not (fatal or restarts or conn):
        out.append(_ev("stat", svc, f"{svc} never restarted, logged nothing fatal and refused no connections",
                       supports=False, weight=4, decisive=True, ref=f"stat:{svc}.no_crash"))
    return out


def test_bad_deploy(ctx: Context, svc: str) -> List[Evidence]:
    lo = ctx.window[0] - 60 * NS
    deploys = [r for r in ctx.events(svc, "deploy") if r.ts_ns >= lo]
    if not deploys:
        return [_ev("stat", svc, f"no deployment event recorded for {svc} near the incident", supports=False,
                    weight=4, decisive=True, ref=f"stat:{svc}.no_deploy")]
    d = deploys[0]
    ver = d.attrs.get("version")
    out = [_ev("log", svc, f"{svc} deployed version {ver} at the start of the incident", weight=4, ts=d.ts_ns, ref=_log_ref(d))]
    errs = [r for r in ctx.w_logs if r.service == svc and r.severity in ("ERROR", "CRITICAL")]
    base_errs = [r for r in ctx.b_logs if r.service == svc and r.severity in ("ERROR", "CRITICAL")]
    if errs and not base_errs and errs[0].ts_ns >= d.ts_ns - NS:
        out.append(_ev("log", svc, f"errors in {svc} begin only after the deploy (none in the baseline)", weight=2,
                       ts=errs[0].ts_ns, ref=_log_ref(errs[0])))
    tagged = [r for r in errs if r.attrs.get("version") == ver]
    if tagged:
        out.append(_ev("log", svc, f"{len(tagged)} errors are tagged with the new version {ver}", weight=2,
                       ts=tagged[0].ts_ns, ref=_log_ref(tagged[0])))
    return out


def test_error(ctx: Context, svc: str) -> List[Evidence]:
    out: List[Evidence] = []
    rate = ctx.err_rate(svc)
    if rate >= 0.15:
        sample = next((s for s in ctx.server(svc) if s.is_error), None)
        out.append(_ev("span", svc, f"{svc} fails {rate:.0%} of its requests (baseline {ctx.err_rate(svc, 'b'):.0%})",
                       weight=3, ts=sample.start_ns if sample else None, ref=_span_ref(sample) if sample else None))
    else:
        out.append(_ev("stat", svc, f"{svc} error rate is only {rate:.0%}", supports=False, weight=3, decisive=True,
                       ref=f"stat:{svc}.err_rate"))
    local = ctx.events(svc, "request_error")
    if local:
        out.append(_ev("log", svc, f"{len(local)} locally generated request errors in {svc}", weight=1,
                       ts=local[0].ts_ns, ref=_log_ref(local[0])))
    errs = [r for r in ctx.w_logs if r.service == svc and r.severity in ("ERROR", "CRITICAL")]
    upstream = [r for r in errs if r.event == "upstream_error"]
    if errs and len(upstream) / len(errs) >= 0.7:
        targets = Counter(r.attrs.get("upstream") for r in upstream).most_common(1)[0][0]
        out.append(_ev("log", svc, f"{len(upstream)} of {len(errs)} error logs in {svc} are failed calls to {targets}: "
                       f"the errors are propagated from {targets}, not generated here", supports=False, weight=4,
                       decisive=True, ts=upstream[0].ts_ns, ref=_log_ref(upstream[0])))
    if ctx.events(svc, "db_pool_exhausted"):
        out.append(_ev("stat", svc, f"{svc}'s errors are explained by database pool exhaustion", supports=False, weight=2,
                       ref=f"stat:{svc}.pool_logs"))
    if [r for r in ctx.events(svc, "deploy") if r.ts_ns >= ctx.window[0] - 60 * NS]:
        out.append(_ev("stat", svc, f"{svc}'s errors coincide with a deployment, so they are better explained by it",
                       supports=False, weight=2, ref=f"stat:{svc}.deploy"))
    return out


TESTS: Dict[str, Callable[[Context, str], List[Evidence]]] = {
    "latency": test_latency, "crash": test_crash, "db_pool_exhaustion": test_db_pool,
    "memory_leak": test_memory_leak, "bad_deploy": test_bad_deploy, "error": test_error,
}


def evaluate(ctx: Context, anomalous: Sequence[str], graph: Dict[Tuple[str, str], Edge],
             root_candidates: Sequence[str], starved: Sequence[str]) -> List[Hypothesis]:
    """Form, test, score and rank every (service, fault) hypothesis."""
    hyps: List[Hypothesis] = []
    for svc in anomalous:
        downstream_anomalous = [c for c in callees(graph, svc) if c in anomalous and c not in starved]
        for ftype in FAULT_TYPES:
            h = Hypothesis(service=svc, fault_type=ftype)
            if svc in root_candidates:
                h.prior = 1.0
                h.evidence.append(_ev("graph", svc, f"{svc} is the deepest anomalous service on the call path",
                                      weight=1, ref="graph:root_candidate"))
            elif svc in starved:
                h.prior = -2.0
                h.evidence.append(_ev("graph", svc, f"{svc} is only seeing less traffic, a symptom of an upstream failure",
                                      supports=False, weight=2, ref="graph:starved"))
            elif downstream_anomalous:
                h.prior = -1.0
                h.evidence.append(_ev("graph", svc, f"{svc}'s anomaly can be explained by its downstream dependency "
                                      f"{downstream_anomalous[0]}", supports=False, weight=1, ref="graph:downstream"))
            h.evidence += TESTS[ftype](ctx, svc)
            h.score = sum(e.weight if e.supports else -e.weight for e in h.evidence)
            decisive = next((e for e in h.evidence if e.decisive and not e.supports), None)
            if decisive:
                h.status, h.rejected_because = "rejected", decisive.summary
            hyps.append(h)

    live = [h for h in hyps if h.status != "rejected"]
    pool = live or hyps
    if pool:
        top = max(h.score for h in pool)
        denom = sum(math.exp((h.score - top) / 1.5) for h in pool)
        for h in pool:
            h.confidence = math.exp((h.score - top) / 1.5) / denom
    hyps.sort(key=lambda h: (h.status == "rejected", -h.score))
    if live and max(h.score for h in live) >= 5:
        best = next(h for h in hyps if h.status != "rejected")
        best.status = "supported"
    return hyps
