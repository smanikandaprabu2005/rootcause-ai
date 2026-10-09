"""Deterministic synthetic telemetry for the demo shop, in the collector's OTLP/JSON-lines format.

Why this exists: it lets the entire pipeline (parser -> store -> analysis -> agents)
be regression-tested and benchmarked in CI without Docker. It models the real
services' behaviour: call chain, 3s upstream timeouts, fault semantics that
survive restarts, OOM kills at the memory limit, and so on.

IMPORTANT: results measured on this simulator are a regression/ablation signal,
NOT the headline benchmark. The headline numbers must come from live runs against
the Docker stack (``python -m rootcause.evaluate --mode live``).
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SERVICES = ["gateway", "orders", "inventory", "payments", "notifications"]
DB_SERVICES = {"orders", "inventory", "payments"}
TIMEOUT_S = 3.0
BASE_RSS_MB = 70.0
MEM_LIMIT_MB = 192.0
CRASH_RESTART_S = 4.0
OOM_RESTART_S = 3.0
T0_DEFAULT = 1_760_000_000.0


@dataclass(frozen=True)
class Scenario:
    name: str
    service: Optional[str]  # None = healthy control run
    fault: Optional[str]
    params: Dict[str, Any] = field(default_factory=dict)


# Keep in sync with faults/inject.py (a unit test enforces this).
SCENARIOS: Dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("latency_payments", "payments", "latency", {"ms": 1500, "jitter_ms": 300}),
        Scenario("crash_inventory", "inventory", "crash", {"after_requests": 20}),
        Scenario("db_pool_orders", "orders", "db_pool_exhaustion", {"hold_seconds": 300}),
        Scenario("memory_leak_payments", "payments", "memory_leak", {"mb_per_request": 3}),
        Scenario("bad_deploy_orders", "orders", "bad_deploy", {"version": "v1.1.0", "error_rate": 0.5}),
        Scenario("error_burst_inventory", "inventory", "error", {"rate": 0.7, "status": 500}),
        Scenario("slow_notifications_trap", "notifications", "latency", {"ms": 4000}),
        Scenario("db_pool_payments", "payments", "db_pool_exhaustion", {"hold_seconds": 300}),
        Scenario("no_fault_control", None, None),
    ]
}


def _attr(key: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _ns(t: float) -> str:
    return str(int(t * 1e9))


class _Sim:
    def __init__(self, sc: Scenario, seed: int, t0: float, baseline_s: float, fault_s: float, post_s: float, rps: float):
        self.sc, self.rng = sc, random.Random(seed)
        self.t0, self.rps = t0, rps
        self.fault_start = t0 + baseline_s
        self.fault_end = self.fault_start + fault_s
        self.t_end = self.fault_end + post_s
        self.spans: Dict[str, List[dict]] = {s: [] for s in SERVICES}
        self.logs: Dict[str, List[dict]] = {s: [] for s in SERVICES}
        self.metrics: Dict[str, List[dict]] = {s: [] for s in SERVICES}
        self.down_until = {s: 0.0 for s in SERVICES}
        self.down_intervals: Dict[str, List[Tuple[float, float]]] = {s: [] for s in SERVICES}
        self.req_count = {s: 0 for s in SERVICES}
        self.leak_mb = {s: 0.0 for s in SERVICES}
        self.rss_events: Dict[str, List[Tuple[float, float]]] = {s: [(t0, 0.0)] for s in SERVICES}

    # ---- primitives ---------------------------------------------------------
    def _id(self, nbytes: int) -> str:
        return "%0*x" % (nbytes * 2, self.rng.getrandbits(nbytes * 8))

    def fault(self, svc: str, t: float) -> Optional[Tuple[str, Dict[str, Any]]]:
        if self.sc.service == svc and self.sc.fault and self.fault_start <= t < self.fault_end:
            return self.sc.fault, self.sc.params
        return None

    def span(self, svc, trace, parent, sid, name, kind, t0, t1, error, *, http=None, url=None, method=None, target=None):
        attrs = [_attr("http.method", method)] if method else []
        if target:
            attrs += [_attr("http.target", target), _attr("http.route", target)]
        if url:
            attrs.append(_attr("http.url", url))
        if http is not None:
            attrs.append(_attr("http.status_code", http))
        rec = {
            "traceId": trace, "spanId": sid, "name": name, "kind": 2 if kind == "server" else 3,
            "startTimeUnixNano": _ns(t0), "endTimeUnixNano": _ns(t1), "attributes": attrs,
            "status": {"code": 2} if error else {"code": 0},
        }
        if parent:
            rec["parentSpanId"] = parent
        self.spans[svc].append(rec)

    def log(self, svc, t, sev, body, event, trace=None, sid=None, **extra):
        rec = {
            "timeUnixNano": _ns(t), "severityText": sev, "body": {"stringValue": body},
            "attributes": [_attr("event", event)] + [_attr(k, v) for k, v in extra.items()],
        }
        if trace:
            rec["traceId"], rec["spanId"] = trace, sid
        self.logs[svc].append(rec)

    def restart(self, svc: str, t_up: float, t_down: float) -> None:
        self.down_until[svc] = max(self.down_until[svc], t_up)
        self.down_intervals[svc].append((t_down, t_up))
        self.req_count[svc] = 0
        self.leak_mb[svc] = 0.0
        self.rss_events[svc].append((t_up, 0.0))
        self.log(svc, t_up, "INFO", "service started", "service_started", version="v1.0.0")

    # ---- request handling ---------------------------------------------------
    def rpc(self, caller, callee, method, path, t, trace, parent_sid) -> Tuple[float, bool]:
        """Returns (time the caller resumes, success)."""
        url = f"http://{callee}:8000{path}"
        csid, ssid = self._id(8), self._id(8)

        def client_fail(t_end, why, http=None):
            self.span(caller, trace, parent_sid, csid, method, "client", t, t_end, True, http=http, url=url, method=method)
            self.log(caller, t_end, "ERROR", f"upstream call to {callee} failed: {why}", "upstream_error",
                     trace, parent_sid, upstream=callee, url=url)
            return t_end, False

        if self.down_until[callee] > t:
            return client_fail(t + 0.003, "HTTPConnectionPool: Max retries exceeded (Connection refused)")
        t_end, status, died = self.serve(callee, method, path, t, trace, csid, ssid)
        if died:
            return client_fail(t + 0.005, "Connection aborted. RemoteDisconnected")
        if t_end - t > TIMEOUT_S:
            return client_fail(t + TIMEOUT_S, "Read timed out. (read timeout=3)")
        if status >= 400:
            return client_fail(t_end + 0.001, f"{status} Server Error", http=status)
        self.span(caller, trace, parent_sid, csid, method, "client", t, t_end + 0.001, False, http=status, url=url, method=method)
        return t_end + 0.001, True

    def serve(self, svc, method, path, t, trace, parent, sid) -> Tuple[float, int, bool]:
        start = t + 0.001
        cur = start
        f = self.fault(svc, start)
        ftype, p = f if f else (None, {})
        self.req_count[svc] += 1
        name = f"{method} {path}"

        def fail(status, event, body, **extra):
            end = cur + 0.002
            self.span(svc, trace, parent, sid, name, "server", start, end, True, http=status, method=method, target=path)
            self.log(svc, end, "ERROR", body, event, trace, sid, **extra)
            return end, status, False

        if ftype == "latency":
            jitter = float(p.get("jitter_ms", 0))
            cur += max(0.0, (float(p["ms"]) + self.rng.uniform(-jitter, jitter)) / 1000.0)
        if ftype == "error" and self.rng.random() < float(p["rate"]):
            return fail(int(p.get("status", 500)), "request_error", f"request failed with {p.get('status', 500)} on {method} {path}")
        if ftype == "bad_deploy" and self.rng.random() < float(p["error_rate"]):
            return fail(500, "request_error", f"request failed with 500 on {method} {path}", version=p["version"])
        if ftype == "memory_leak":
            self.leak_mb[svc] += float(p["mb_per_request"])
            self.rss_events[svc].append((cur, self.leak_mb[svc]))
            if BASE_RSS_MB + self.leak_mb[svc] > MEM_LIMIT_MB:  # kernel OOM-kill: no log from the dying process
                self.restart(svc, cur + OOM_RESTART_S, cur)
                return cur, 0, True
        if ftype == "crash" and self.req_count[svc] > int(p.get("after_requests", 0)):
            self.log(svc, cur, "CRITICAL", "fatal: unrecoverable state, exiting", "crash", trace, sid)
            self.restart(svc, cur + CRASH_RESTART_S, cur)
            return cur, 0, True
        if ftype == "db_pool_exhaustion" and svc in DB_SERVICES:
            cur += 2.0
            return fail(503, "db_pool_exhausted",
                        "database connection pool exhausted: waited 2.0s for one of 5 connections")

        end, status = self.handle(svc, method, path, cur, trace, sid)
        self.span(svc, trace, parent, sid, name, "server", start, end, status >= 500, http=status, method=method, target=path)
        return end, status, False

    def handle(self, svc, method, path, t, trace, sid) -> Tuple[float, int]:
        jit = lambda lo, hi: self.rng.uniform(lo, hi)  # noqa: E731
        if svc == "gateway":
            if path == "/products":
                t, ok = self.rpc("gateway", "inventory", "GET", "/items", t + 0.001, trace, sid)
            else:
                t, ok = self.rpc("gateway", "orders", "POST", "/orders", t + 0.001, trace, sid)
            return t + 0.001, 200 if ok else 502
        if svc == "orders":
            t += jit(0.004, 0.008)
            t, ok = self.rpc("orders", "inventory", "POST", "/reserve", t, trace, sid)
            if not ok:
                return t, 502
            t, ok = self.rpc("orders", "payments", "POST", "/charge", t, trace, sid)
            if not ok:
                return t, 502
            t += jit(0.004, 0.008)
            t, ok = self.rpc("orders", "notifications", "POST", "/notify", t, trace, sid)
            if not ok:
                self.log("orders", t, "WARNING", "order confirmed but notification failed", "notify_skipped", trace, sid)
            self.log("orders", t, "INFO", "order confirmed", "order_confirmed", trace, sid)
            return t + 0.001, 200
        if svc == "inventory":
            return t + jit(0.006, 0.012), 200
        if svc == "payments":
            end = t + jit(0.018, 0.035)
            self.log("payments", end, "INFO", "payment captured", "payment_captured", trace, sid)
            return end, 200
        end = t + jit(0.004, 0.010)  # notifications
        self.log("notifications", end, "INFO", "notification sent", "notified", trace, sid)
        return end, 200

    # ---- run ----------------------------------------------------------------
    def run(self) -> None:
        for svc in SERVICES:
            self.log(svc, self.t0, "INFO", "service started", "service_started", version="v1.0.0")
        if self.sc.fault == "bad_deploy":
            self.log(self.sc.service, self.fault_start, "WARNING", "deployment rolled out", "deploy",
                     version=self.sc.params["version"])
        t = self.t0 + 1.0
        while t < self.t_end:
            trace, sid = self._id(16), self._id(8)
            path = "/products" if self.rng.random() < 0.15 else "/orders"
            method = "GET" if path == "/products" else "POST"
            end, status, _ = self.serve("gateway", method, path, t, trace, None, sid)
            t += self.rng.expovariate(self.rps)
        self._sample_metrics()

    def _rss_at(self, svc: str, t: float) -> float:
        leak = 0.0
        for ts, mb in self.rss_events[svc]:
            if ts <= t:
                leak = mb
        return BASE_RSS_MB + leak + self.rng.uniform(-1.5, 1.5)

    def _sample_metrics(self) -> None:
        t = self.t0
        while t < self.t_end:
            for svc in SERVICES:
                if any(a <= t < b for a, b in self.down_intervals[svc]):
                    continue
                self.metrics[svc].append(self._gauge("process.memory.rss", t, self._rss_at(svc, t) * 1024 * 1024, False))
                if svc in DB_SERVICES:
                    f = self.fault(svc, t)
                    in_use = 5 if f and f[0] == "db_pool_exhaustion" else self.rng.choice([0, 0, 1, 1, 2])
                    self.metrics[svc].append(self._gauge("db.pool.in_use", t, in_use, True))
            t += 5.0

    @staticmethod
    def _gauge(name: str, t: float, value: float, is_int: bool) -> dict:
        dp = {"timeUnixNano": _ns(t), "attributes": []}
        dp["asInt" if is_int else "asDouble"] = str(int(value)) if is_int else value
        return {"name": name, "gauge": {"dataPoints": [dp]}}

    # ---- output -------------------------------------------------------------
    def write(self, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        resource = lambda svc: {"attributes": [_attr("service.name", svc)]}  # noqa: E731
        with open(out / "traces.jsonl", "w") as fh:
            for svc, items in self.spans.items():
                items.sort(key=lambda s: int(s["startTimeUnixNano"]))
                for i in range(0, len(items), 500):
                    fh.write(json.dumps({"resourceSpans": [{"resource": resource(svc),
                             "scopeSpans": [{"spans": items[i:i + 500]}]}]}) + "\n")
        with open(out / "logs.jsonl", "w") as fh:
            for svc, items in self.logs.items():
                items.sort(key=lambda r: int(r["timeUnixNano"]))
                for i in range(0, len(items), 500):
                    fh.write(json.dumps({"resourceLogs": [{"resource": resource(svc),
                             "scopeLogs": [{"logRecords": items[i:i + 500]}]}]}) + "\n")
        with open(out / "metrics.jsonl", "w") as fh:
            for svc, items in self.metrics.items():
                fh.write(json.dumps({"resourceMetrics": [{"resource": resource(svc),
                         "scopeMetrics": [{"metrics": items}]}]}) + "\n")


def generate(scenario: str, out_dir: str | Path, seed: int = 0, t0: float = T0_DEFAULT,
             baseline_s: float = 150.0, fault_s: float = 90.0, post_s: float = 30.0, rps: float = 5.0) -> Dict[str, Any]:
    """Write one scenario's telemetry to ``out_dir`` and return its ground-truth record."""
    sc = SCENARIOS[scenario]
    sim = _Sim(sc, seed, t0, baseline_s, fault_s, post_s, rps)
    sim.run()
    sim.write(Path(out_dir))
    return {
        "scenario": scenario, "root_cause_service": sc.service, "fault_type": sc.fault,
        "start": sim.fault_start, "end": sim.fault_end, "t0": t0, "t_end": sim.t_end, "seed": seed,
    }
