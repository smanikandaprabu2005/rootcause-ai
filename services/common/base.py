"""Shared scaffolding for the demo microservices.

Every service gets, from one ``make_app`` call:
  * OpenTelemetry traces, metrics and logs exported over OTLP/HTTP
  * structured JSON logs that carry trace_id / span_id
  * a runtime fault-injection API (``/admin/fault``) used by the chaos harness
  * an optional Postgres connection pool whose exhaustion can be simulated
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

FAULT_TYPES = {
    "latency",             # params: ms, jitter_ms, probability
    "error",               # params: rate, status
    "crash",               # params: after_requests
    "memory_leak",         # params: mb_per_request
    "db_pool_exhaustion",  # params: hold_seconds
    "bad_deploy",          # params: version, error_rate
}
LEAK_CAP_MB = int(os.getenv("LEAK_CAP_MB", "2048"))


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
# Structured fields are passed as flat `extra={...}` keys (not a nested dict):
# the OTLP log exporter drops dict-valued attributes, flat scalars survive.
_STD_LOG_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")
        for key, value in record.__dict__.items():  # flat `extra=` attributes
            if key not in _STD_LOG_ATTRS and not key.startswith("_") and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(service: str, resource: Resource, endpoint: Optional[str]) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter(service))
    root.addHandler(stream)

    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

            provider = LoggerProvider(resource=resource)
            provider.add_log_record_processor(
                BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
            )
            root.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
        except ImportError:  # keep services usable if the log SDK moves
            root.warning("OTLP log export unavailable; stdout logs only")
    return logging.getLogger(service)


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
def _rss_bytes() -> int:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def setup_telemetry(service: str, version: str, endpoint: Optional[str], db: "Optional[Db]") -> Resource:
    resource = Resource.create({"service.name": service, "service.version": version})

    tracer_provider = TracerProvider(resource=resource)
    readers = []
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
        )
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
                export_interval_millis=int(os.getenv("METRIC_INTERVAL_MS", "5000")),
            )
        )
    trace.set_tracer_provider(tracer_provider)
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(meter_provider)

    meter = metrics.get_meter(service)
    meter.create_observable_gauge(
        "process.memory.rss",
        callbacks=[lambda _o: [Observation(_rss_bytes())]],
        unit="By",
        description="Resident set size of the service process",
    )
    if db is not None:
        meter.create_observable_gauge(
            "db.pool.in_use",
            callbacks=[lambda _o: [Observation(db.in_use)]],
            description="Database connections currently checked out",
        )
    RequestsInstrumentor().instrument()
    return resource


# --------------------------------------------------------------------------- #
# Database pool (bounded, with timeout so exhaustion is observable)
# --------------------------------------------------------------------------- #
class PoolExhausted(Exception):
    pass


class Db:
    def __init__(self, dsn: str, maxconn: int = 5, acquire_timeout: float = 2.0):
        import psycopg2.pool

        self.maxconn = maxconn
        self.acquire_timeout = acquire_timeout
        self.in_use = 0
        self._sem = threading.BoundedSemaphore(maxconn)
        self._count_lock = threading.Lock()
        self._release_hold = threading.Event()
        self._pool = None
        deadline = time.time() + 60
        while True:  # wait for postgres to accept connections
            try:
                self._pool = psycopg2.pool.ThreadedConnectionPool(1, maxconn, dsn)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(1)

    @contextmanager
    def conn(self):
        if not self._sem.acquire(timeout=self.acquire_timeout):
            raise PoolExhausted(
                f"database connection pool exhausted: waited {self.acquire_timeout}s "
                f"for one of {self.maxconn} connections"
            )
        with self._count_lock:
            self.in_use += 1
        connection = self._pool.getconn()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._pool.putconn(connection)
            with self._count_lock:
                self.in_use -= 1
            self._sem.release()

    # Fault helpers: occupy every slot, like a leak or a stuck long transaction.
    def hold_all(self, seconds: float) -> None:
        self._release_hold.clear()

        def _hold() -> None:
            taken = 0
            for _ in range(self.maxconn):
                if self._sem.acquire(timeout=30):
                    taken += 1
                    with self._count_lock:
                        self.in_use += 1
            self._release_hold.wait(timeout=seconds)
            for _ in range(taken):
                with self._count_lock:
                    self.in_use -= 1
                self._sem.release()

        threading.Thread(target=_hold, daemon=True, name="db-hold").start()

    def release_held(self) -> None:
        self._release_hold.set()


# --------------------------------------------------------------------------- #
# Fault state
# --------------------------------------------------------------------------- #
class FaultState:
    def __init__(self, service: str, base_version: str, db: Optional[Db], logger: logging.Logger,
                 state_file: Optional[str] = None):
        self.state_file = state_file
        self.service = service
        self.base_version = base_version
        self.version = base_version
        self.db = db
        self.log = logger
        self._lock = threading.Lock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._leak: list = []
        self._requests_seen = 0

    # Faults survive a container restart (like a real defect would), so a crash
    # fault produces a crash *loop* and a leak keeps leaking after an OOM-kill.
    def _persist(self) -> None:
        if not self.state_file:
            return
        try:
            with open(self.state_file, "w") as fh:
                json.dump({k: {a: b for a, b in v.items() if a != "since"} for k, v in self.snapshot().items()}, fh)
        except OSError:
            pass

    def restore(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        for ftype, params in saved.items():
            try:
                self.apply(ftype, params)
            except ValueError:
                pass

    def apply(self, ftype: str, params: Dict[str, Any]) -> None:
        if ftype not in FAULT_TYPES:
            raise ValueError(f"unknown fault type '{ftype}'")
        with self._lock:
            self._active[ftype] = {**params, "since": time.time()}
            self._requests_seen = 0
        if ftype == "db_pool_exhaustion":
            if self.db is None:
                raise ValueError(f"{self.service} has no database")
            self.db.hold_all(float(params.get("hold_seconds", 120)))
        if ftype == "bad_deploy":
            self.version = str(params.get("version", "v2.0.0-bad"))
            self.log.warning(
                "deployment rolled out", extra={"event": "deploy", "version": self.version}
            )
        # Debug only: the investigator must never be handed the injected fault's name.
        self.log.debug("fault state changed", extra={"event": "fault_state_changed"})
        self._persist()

    def clear(self, ftype: Optional[str] = None) -> None:
        with self._lock:
            targets = [ftype] if ftype else list(self._active)
            for t in targets:
                self._active.pop(t, None)
            if ftype in (None, "memory_leak"):
                self._leak.clear()
        if ftype in (None, "db_pool_exhaustion") and self.db is not None:
            self.db.release_held()
        if ftype in (None, "bad_deploy"):
            self.version = self.base_version
        self.log.debug("fault state changed", extra={"event": "fault_state_changed"})
        self._persist()

    # Remediation actions the Fixer agent may request (verified in a sandbox first).
    # Each maps to the runtime fault it neutralises; the demo's faults stand in for
    # real underlying causes, so "rollback" etc. are the operator-level actions.
    REMEDIATIONS = {
        "rollback_deploy": "bad_deploy",
        "reset_db_pool": "db_pool_exhaustion",
        "restart_service": "memory_leak",
        "hotfix_crash": "crash",
        "clear_error_source": "error",
        "failover_dependency": "latency",
    }

    def remediate(self, action: str) -> str:
        if action not in self.REMEDIATIONS:
            raise ValueError(f"unknown remediation '{action}'")
        self.clear(self.REMEDIATIONS[action])
        self.log.warning(f"remediation applied: {action}", extra={"event": "remediation", "action": action})
        return self.REMEDIATIONS[action]

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._active.items()}

    # ---- per-request effects ------------------------------------------------
    def latency_seconds(self) -> float:
        f = self.snapshot().get("latency")
        if not f or random.random() > float(f.get("probability", 1.0)):
            return 0.0
        jitter = float(f.get("jitter_ms", 0))
        return max(0.0, (float(f.get("ms", 1000)) + random.uniform(-jitter, jitter)) / 1000.0)

    def error_status(self) -> Optional[int]:
        snap = self.snapshot()
        if "error" in snap and random.random() < float(snap["error"].get("rate", 0.5)):
            return int(snap["error"].get("status", 500))
        if "bad_deploy" in snap and random.random() < float(snap["bad_deploy"].get("error_rate", 0.5)):
            return 500
        return None

    def leak_step(self) -> None:
        f = self.snapshot().get("memory_leak")
        if not f:
            return
        mb = float(f.get("mb_per_request", 2))
        if sum(len(b) for b in self._leak) < LEAK_CAP_MB * 1024 * 1024:
            self._leak.append(b"x" * int(mb * 1024 * 1024))

    def should_crash(self) -> bool:
        f = self.snapshot().get("crash")
        if not f:
            return False
        with self._lock:
            self._requests_seen += 1
            return self._requests_seen > int(f.get("after_requests", 0))


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def make_app(service: str, use_db: bool = False) -> SimpleNamespace:
    version = os.getenv("SERVICE_VERSION", "v1.0.0")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    db = Db(os.environ["DATABASE_URL"], maxconn=int(os.getenv("DB_POOL_SIZE", "5"))) if use_db else None
    resource = setup_telemetry(service, version, endpoint, db)
    logger = setup_logging(service, resource, endpoint)
    faults = FaultState(
        service, version, db, logger,
        state_file=os.getenv("FAULT_STATE_FILE", f"/tmp/{service}_faults.json"),
    )
    faults.restore()

    app = FastAPI(title=service)

    @app.exception_handler(PoolExhausted)
    async def _pool_exhausted(_req: Request, exc: PoolExhausted):
        logger.error(str(exc), extra={"event": "db_pool_exhausted"})
        return JSONResponse({"detail": "database unavailable"}, status_code=503)

    @app.middleware("http")
    async def _fault_middleware(request: Request, call_next):
        path = request.url.path
        if path.startswith(("/admin", "/health")):
            return await call_next(request)
        delay = faults.latency_seconds()
        if delay:
            await asyncio.sleep(delay)
        status = faults.error_status()
        if status:
            logger.error(
                f"request failed with {status} on {request.method} {path}",
                extra={"event": "request_error", "status": status, "version": faults.version},
            )
            resp = JSONResponse({"detail": "internal error"}, status_code=status)
            resp.headers["x-service-version"] = faults.version
            return resp
        faults.leak_step()
        if faults.should_crash():
            logger.critical("fatal: unrecoverable state, exiting", extra={"event": "crash"})
            for handler in logging.getLogger().handlers:
                handler.flush()
            os._exit(1)
        response = await call_next(request)
        response.headers["x-service-version"] = faults.version
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "service": service, "version": faults.version}

    @app.get("/admin/fault")
    def get_faults():
        return {"service": service, "active": faults.snapshot(), "version": faults.version}

    @app.post("/admin/fault")
    def set_fault(body: Dict[str, Any]):
        try:
            faults.apply(str(body.get("type")), body.get("params") or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"service": service, "active": faults.snapshot()}

    @app.delete("/admin/fault")
    def clear_faults(type: Optional[str] = None):
        faults.clear(type)
        return {"service": service, "active": faults.snapshot()}

    @app.post("/admin/remediate")
    def remediate(body: Dict[str, Any]):
        try:
            cleared = faults.remediate(str(body.get("action")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"service": service, "cleared": cleared, "active": faults.snapshot()}

    # Instrument last so the span wraps the fault middleware: injected latency
    # and errors then appear in traces exactly like real ones would.
    FastAPIInstrumentor.instrument_app(app)

    logger.info("service started", extra={"event": "service_started", "version": version})

    timeout = float(os.getenv("UPSTREAM_TIMEOUT", "3"))

    def upstream(name: str, method: str, url: str, **kwargs: Any) -> Any:
        """Call another service; failures become 502s and are logged with the target."""
        kwargs.setdefault("timeout", timeout)
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error(
                f"upstream call to {name} failed: {exc}",
                extra={"event": "upstream_error", "upstream": name, "url": url},
            )
            raise HTTPException(status_code=502, detail=f"{name} unavailable")

    return SimpleNamespace(app=app, logger=logger, faults=faults, db=db, upstream=upstream, version=version)
