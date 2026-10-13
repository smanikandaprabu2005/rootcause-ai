"""Docker-free stack: the *real* demo services as local processes, for laptops and CI without Docker.

It reproduces what ``docker compose up`` provides, so everything downstream (ingestion, the investigator, the
live benchmark, the sandboxed remediation check) runs unchanged against real telemetry:

  * the five FastAPI services, real OpenTelemetry SDK, real HTTP calls between them, real fault injection
  * an OTLP/HTTP receiver that writes the same JSON-lines files as the OpenTelemetry Collector's file exporter
  * a supervisor that restarts crashed services with Docker's backoff and kills a service that exceeds its
    memory limit (the services' ``mem_limit: 192m`` in docker-compose.yml)
  * the load generator
  * SQLite instead of MySQL (the pool, its limits and the exhaustion fault behave identically)

    python -m rootcause.devstack up                 # ports 8080-8084, telemetry in data/devstack/telemetry
    python -m rootcause.devstack up --offset 10000  # a second, disposable copy on ports 18080-18084

Requires ``pip install -r services/requirements.txt``.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ["gateway", "orders", "inventory", "payments", "notifications"]
BASE_PORTS = {"gateway": 8080, "orders": 8081, "inventory": 8082, "payments": 8083, "notifications": 8084}
SINK_PORT = 4318
HOSTS_MARK = "# rootcause-devstack"


# --------------------------------------------------------------------------- OTLP receiver
class _Sink(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, outdir: Path):
        from opentelemetry.proto.collector.logs.v1 import logs_service_pb2 as logs_pb
        from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2 as metrics_pb
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as trace_pb

        self.kinds = {
            "/v1/traces": ("traces", trace_pb.ExportTraceServiceRequest, trace_pb.ExportTraceServiceResponse),
            "/v1/metrics": ("metrics", metrics_pb.ExportMetricsServiceRequest, metrics_pb.ExportMetricsServiceResponse),
            "/v1/logs": ("logs", logs_pb.ExportLogsServiceRequest, logs_pb.ExportLogsServiceResponse),
        }
        self.outdir = outdir
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", port), _SinkHandler)

    def write(self, kind: str, doc: dict) -> None:
        with self.lock, open(self.outdir / f"{kind}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, separators=(",", ":")) + "\n")


def _hexify(records: List[dict], *keys: str) -> None:
    for r in records:
        for k in keys:
            if r.get(k):
                r[k] = base64.b64decode(r[k]).hex()  # protobuf JSON has base64 ids; the Collector writes hex


class _SinkHandler(BaseHTTPRequestHandler):
    server: _Sink

    def log_message(self, *args) -> None:  # silence per-request logging
        pass

    def do_POST(self) -> None:  # noqa: N802
        from google.protobuf.json_format import MessageToDict

        entry = self.server.kinds.get(self.path)
        if entry is None:
            self.send_error(404)
            return
        kind, req_cls, resp_cls = entry
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        try:
            req = req_cls()
            req.ParseFromString(body)
            doc = MessageToDict(req)
        except Exception:
            self.send_error(400)
            return
        for rs in doc.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                _hexify(ss.get("spans", []), "traceId", "spanId", "parentSpanId")
        for rl in doc.get("resourceLogs", []):
            for sl in rl.get("scopeLogs", []):
                _hexify(sl.get("logRecords", []), "traceId", "spanId")
        self.server.write(kind, doc)
        out = resp_cls().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


# --------------------------------------------------------------------------- supervisor
def _rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


@dataclass
class _Svc:
    name: str
    port: int
    proc: Optional[subprocess.Popen] = None
    started_at: float = 0.0
    backoff: float = 0.1
    next_start: float = 0.0
    restarts: int = 0
    killed_for_memory: int = 0
    log: Optional[object] = field(default=None, repr=False)


def ensure_hosts() -> bool:
    """Make ``orders``, ``payments`` ... resolve locally so spans carry the same peer names as under Compose."""
    try:
        text = Path("/etc/hosts").read_text()
        if all(f" {n}" in text or f"\t{n}" in text for n in SERVICES):
            return True
        with open("/etc/hosts", "a") as fh:
            fh.write(f"127.0.0.1 {' '.join(SERVICES)} {HOSTS_MARK}\n")
        return True
    except OSError:
        return False


class DevStack:
    def __init__(self, workdir: Optional[str | Path] = None, offset: int = 0, loadgen: bool = True,
                 rate: float = 5.0, mem_limit_mb: float = 192.0, quiet: bool = False):
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="rc-devstack-"))
        self._own_workdir = workdir is None
        self.offset, self.rate, self.mem_limit_mb, self.quiet = offset, rate, mem_limit_mb, quiet
        self.with_loadgen = loadgen
        self.ports = {n: p + offset for n, p in BASE_PORTS.items()}
        self.sink_port = SINK_PORT + offset
        self.telemetry_dir = self.workdir / "telemetry"
        self.svcs: Dict[str, _Svc] = {n: _Svc(n, p) for n, p in self.ports.items()}
        self._stop = threading.Event()
        self._sink: Optional[_Sink] = None
        self._loadgen: Optional[subprocess.Popen] = None
        self._threads: List[threading.Thread] = []
        self.events: List[str] = []
        self.hosts_ok = False

    # ---- helpers ------------------------------------------------------------
    def _log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.events.append(line)
        if not self.quiet:
            print(line, flush=True)

    def _host(self, name: str) -> str:
        return name if self.hosts_ok else "localhost"

    def _env(self, name: str) -> Dict[str, str]:
        env = dict(os.environ)
        for k in ("DATABASE_URL", "OTEL_EXPORTER_OTLP_ENDPOINT"):
            env.pop(k, None)
        env.update({
            "PYTHONPATH": str(ROOT / "services"), "PYTHONUNBUFFERED": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{self.sink_port}",
            "DATABASE_URL": f"sqlite:///{self.workdir / 'shop.db'}",
            "FAULT_STATE_FILE": str(self.workdir / "state" / f"{name}_faults.json"),
            "DB_POOL_SIZE": "5", "UPSTREAM_TIMEOUT": "3", "METRIC_INTERVAL_MS": "5000",
            "ORDERS_URL": f"http://{self._host('orders')}:{self.ports['orders']}",
            "INVENTORY_URL": f"http://{self._host('inventory')}:{self.ports['inventory']}",
            "PAYMENTS_URL": f"http://{self._host('payments')}:{self.ports['payments']}",
            "NOTIFICATIONS_URL": f"http://{self._host('notifications')}:{self.ports['notifications']}",
        })
        return env

    def _spawn(self, s: _Svc) -> None:
        if s.log is None:
            s.log = open(self.workdir / "logs" / f"{s.name}.log", "ab")
        s.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(ROOT / "services" / s.name),
             "--host", "127.0.0.1", "--port", str(s.port), "--no-access-log"],
            env=self._env(s.name), stdout=s.log, stderr=subprocess.STDOUT, start_new_session=True)
        s.started_at = time.time()

    def _supervise(self) -> None:
        last_mem_check = 0.0
        while not self._stop.is_set():
            now = time.time()
            check_mem = now - last_mem_check >= 1.0
            if check_mem:
                last_mem_check = now
            for s in self.svcs.values():
                if s.proc is None:
                    continue
                code = s.proc.poll()
                if code is None:
                    if check_mem and _rss_mb(s.proc.pid) > self.mem_limit_mb:
                        self._log(f"{s.name}: exceeded {self.mem_limit_mb:.0f} MB, OOM-killed")
                        s.killed_for_memory += 1
                        try:
                            os.killpg(s.proc.pid, signal.SIGKILL)
                        except OSError:
                            pass
                    continue
                if s.next_start == 0.0:  # just noticed the exit: schedule a restart like `restart: unless-stopped`
                    uptime = now - s.started_at
                    s.backoff = 0.1 if uptime > 10 else min(s.backoff * 2, 60.0)
                    s.next_start = now + s.backoff
                    self._log(f"{s.name}: exited with code {code} after {uptime:.1f}s, restarting in {s.backoff:.1f}s")
                elif now >= s.next_start:
                    s.next_start = 0.0
                    s.restarts += 1
                    self._spawn(s)
            time.sleep(0.2)

    # ---- lifecycle ----------------------------------------------------------
    def start(self, timeout: float = 90.0) -> "DevStack":
        for d in (self.telemetry_dir, self.workdir / "state", self.workdir / "logs"):
            d.mkdir(parents=True, exist_ok=True)
        for f in list(self.telemetry_dir.glob("*.jsonl")) + list((self.workdir / "state").glob("*.json")):
            f.unlink()  # a fresh stack starts healthy, with no leftover faults
        self.hosts_ok = ensure_hosts()
        if not self.hosts_ok:
            self._log("warning: /etc/hosts is not writable; using 'localhost' (peer names will not match Compose)")
        self._sink = _Sink(self.sink_port, self.telemetry_dir)
        t = threading.Thread(target=self._sink.serve_forever, daemon=True, name="otlp-sink")
        t.start()
        self._threads.append(t)
        for s in self.svcs.values():
            self._spawn(s)
        sup = threading.Thread(target=self._supervise, daemon=True, name="supervisor")
        sup.start()
        self._threads.append(sup)
        self._wait_healthy(timeout)
        if self.with_loadgen:
            self._loadgen = subprocess.Popen(
                [sys.executable, str(ROOT / "loadgen" / "loadgen.py")], start_new_session=True,
                env={**os.environ, "GATEWAY_URL": f"http://127.0.0.1:{self.ports['gateway']}", "RATE": str(self.rate)},
                stdout=open(self.workdir / "logs" / "loadgen.log", "ab"), stderr=subprocess.STDOUT)
        self._log(f"stack ready: gateway http://127.0.0.1:{self.ports['gateway']}, telemetry in {self.telemetry_dir}")
        return self

    def _wait_healthy(self, timeout: float) -> None:
        deadline = time.time() + timeout
        pending = set(self.svcs)
        while pending and time.time() < deadline:
            for name in list(pending):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{self.ports[name]}/health", timeout=1).read()
                    pending.discard(name)
                except (urllib.error.URLError, OSError):
                    pass
            time.sleep(0.3)
        if pending:
            tails = {n: (self.workdir / "logs" / f"{n}.log").read_text(errors="replace")[-600:] for n in pending}
            raise RuntimeError(f"services did not become healthy: {sorted(pending)}\n{json.dumps(tails, indent=1)}")

    def stop(self) -> None:
        self._stop.set()
        procs = [s.proc for s in self.svcs.values() if s.proc] + ([self._loadgen] if self._loadgen else [])
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 4
        for p in procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
        if self._sink:
            self._sink.shutdown()
            self._sink.server_close()
        for s in self.svcs.values():
            if s.log:
                s.log.close()
        if self._own_workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> "DevStack":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="start the stack and keep it running until interrupted")
    up.add_argument("--workdir", default=str(ROOT / "data" / "devstack"))
    up.add_argument("--offset", type=int, default=0, help="add to every port (10000 -> a second copy on 18080-18084)")
    up.add_argument("--no-loadgen", action="store_true")
    up.add_argument("--rate", type=float, default=5.0)
    args = ap.parse_args()

    stack = DevStack(args.workdir, args.offset, loadgen=not args.no_loadgen, rate=args.rate)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        stack.start()
        (stack.workdir / "READY").write_text(str(os.getpid()))
        print(f"READY pid={os.getpid()} telemetry={stack.telemetry_dir}", flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stack.stop()
        try:
            (stack.workdir / "READY").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
