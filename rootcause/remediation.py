"""Remediation agent: propose a fix for the diagnosed root cause, then PROVE it
works in a disposable copy of the stack before anyone touches production.

The agent never acts on the live system. It reproduces the fault in a throwaway
Docker Compose project, applies the proposed action, and checks that the failure
actually goes away. The result (verified / not verified) is attached to the report
and the production action stays a human decision.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

ROOT = Path(__file__).resolve().parent.parent

# fault_type -> (admin action understood by the demo services, runbook text)
PLAYBOOK: Dict[str, Tuple[str, str]] = {
    "bad_deploy": ("rollback_deploy", "Roll {svc} back to the previous release; errors began with the deploy."),
    "db_pool_exhaustion": ("reset_db_pool", "Release leaked connections in {svc} (restart its pool), then audit "
                           "code paths that hold connections across slow calls."),
    "memory_leak": ("restart_service", "Restart {svc} to reclaim memory now; ship a fix for the leak and set a "
                    "memory-based alert below the OOM limit."),
    "crash": ("hotfix_crash", "Roll out a hotfix for the crash path in {svc}; add a restart-rate alert."),
    "error": ("clear_error_source", "Isolate the failing dependency path in {svc} (disable the flaky code path or "
              "fail over) while the error source is fixed."),
    "latency": ("failover_dependency", "Fail over or scale {svc}; add a timeout and circuit breaker in its callers "
                "so slowness stops propagating."),
}

# Parameters used to reproduce each fault type in the sandbox.
REPRO_PARAMS: Dict[str, Dict[str, Any]] = {
    "latency": {"ms": 1500, "jitter_ms": 200},
    "crash": {"after_requests": 5},
    "db_pool_exhaustion": {"hold_seconds": 300},
    "memory_leak": {"mb_per_request": 6},
    "bad_deploy": {"version": "v-sandbox-bad", "error_rate": 0.6},
    "error": {"rate": 0.7, "status": 500},
}

Measurement = Dict[str, float]  # {"error_rate": ..., "p95_ms": ...}


class Sandbox(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def measure(self) -> Measurement: ...
    def inject(self, service: str, fault_type: str, params: Dict[str, Any]) -> None: ...
    def remediate(self, service: str, action: str) -> None: ...


def propose(diagnosis: Dict[str, Any]) -> Optional[Dict[str, str]]:
    ftype, svc = diagnosis.get("fault_type"), diagnosis.get("service")
    if not ftype or not svc or ftype not in PLAYBOOK:
        return None
    action, text = PLAYBOOK[ftype]
    return {"action": action, "service": svc, "fault_type": ftype, "description": text.format(svc=svc)}


class DockerComposeSandbox:
    """A second, isolated copy of the demo stack (different project name and ports)."""

    PORTS = {"gateway": 18080, "orders": 18081, "inventory": 18082, "payments": 18083, "notifications": 18084}

    def __init__(self, project: str = "rc-sandbox", requests_per_measure: int = 60, rps: float = 12.0):
        self.project, self.n, self.rps = project, requests_per_measure, rps
        self._cmd = ["docker", "compose", "-p", project, "-f", str(ROOT / "docker-compose.yml"),
                     "-f", str(ROOT / "docker-compose.sandbox.yml")]

    @staticmethod
    def available() -> bool:
        if not shutil.which("docker"):
            return False
        try:
            return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def start(self) -> None:
        subprocess.run(self._cmd + ["up", "-d", "--build", "--wait", "--wait-timeout", "180"], check=True,
                       capture_output=True, cwd=ROOT)

    def stop(self) -> None:
        subprocess.run(self._cmd + ["down", "-v", "--remove-orphans"], capture_output=True, cwd=ROOT)

    def _admin(self, service: str, method: str, path: str, body: Optional[dict] = None) -> None:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://localhost:{self.PORTS[service]}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()

    def inject(self, service: str, fault_type: str, params: Dict[str, Any]) -> None:
        self._admin(service, "POST", "/admin/fault", {"type": fault_type, "params": params})

    def remediate(self, service: str, action: str) -> None:
        self._admin(service, "POST", "/admin/remediate", {"action": action})

    def measure(self) -> Measurement:
        lat, failed = [], 0
        for i in range(self.n):
            t = time.time()
            body = json.dumps({"item_id": (i % 20) + 1, "qty": 1}).encode()
            req = urllib.request.Request(f"http://localhost:{self.PORTS['gateway']}/orders", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=6).read()
            except (urllib.error.URLError, OSError):
                failed += 1
            lat.append((time.time() - t) * 1000)
            time.sleep(max(0.0, 1 / self.rps - (time.time() - t)))
        lat.sort()
        return {"error_rate": failed / self.n, "p95_ms": lat[int(0.95 * (len(lat) - 1))]}


class NativeSandbox(DockerComposeSandbox):
    """The same disposable-copy idea without containers: a second set of the real demo services as local
    processes (ports 18080+, its own temp directory, database and telemetry), torn down afterwards.

    Isolation is process-level rather than container-level, but it is the real services with the real fault
    injection, so "the failure went away" is measured, not assumed."""

    def __init__(self, requests_per_measure: int = 60, rps: float = 12.0):
        self.project, self.n, self.rps = "native", requests_per_measure, rps
        self._stack = None

    @staticmethod
    def available() -> bool:
        try:
            import opentelemetry.sdk  # noqa: F401
            import uvicorn  # noqa: F401
        except ImportError:
            return False
        return True

    def start(self) -> None:
        from .devstack import DevStack

        self._stack = DevStack(offset=10000, loadgen=False, quiet=True).start()

    def stop(self) -> None:
        if self._stack is not None:
            self._stack.stop()
            self._stack = None


def verify(proposal: Dict[str, str], sandbox: Sandbox) -> Dict[str, Any]:
    """Reproduce the fault, apply the fix, and check that service health recovers."""
    steps: List[str] = []
    svc, ftype, action = proposal["service"], proposal["fault_type"], proposal["action"]
    try:
        sandbox.start()
        steps.append("started disposable sandbox stack")
        healthy = sandbox.measure()
        steps.append(f"baseline: {healthy['error_rate']:.0%} errors, p95 {healthy['p95_ms']:.0f} ms")
        sandbox.inject(svc, ftype, REPRO_PARAMS[ftype])
        steps.append(f"reproduced {ftype} in {svc}")
        degraded = sandbox.measure()
        steps.append(f"degraded: {degraded['error_rate']:.0%} errors, p95 {degraded['p95_ms']:.0f} ms")
        reproduced = (degraded["error_rate"] >= max(0.05, healthy["error_rate"] + 0.05)
                      or degraded["p95_ms"] >= max(2 * healthy["p95_ms"], healthy["p95_ms"] + 200))
        if not reproduced:
            # The fix was never tried, so this is "unverified", not "failed".
            return {"verified": None, "reason": "could not reproduce the failure in the sandbox, so the fix was "
                    "not tested", "steps": steps, "before": degraded, "after": None, "baseline": healthy}
        sandbox.remediate(svc, action)
        steps.append(f"applied '{action}' to {svc}")
        time.sleep(1.0)
        after = sandbox.measure()
        steps.append(f"after fix: {after['error_rate']:.0%} errors, p95 {after['p95_ms']:.0f} ms")
        recovered = (after["error_rate"] <= max(0.05, healthy["error_rate"] + 0.02)
                     and after["p95_ms"] <= max(2 * healthy["p95_ms"], healthy["p95_ms"] + 150))
        return {"verified": bool(recovered), "steps": steps, "baseline": healthy, "before": degraded, "after": after,
                "reason": None if recovered else "service did not return to baseline health after the fix"}
    except Exception as exc:  # sandbox infrastructure errors are reported, never raised
        steps.append(f"sandbox error: {exc}")
        return {"verified": None, "reason": f"sandbox failed: {exc}", "steps": steps}
    finally:
        try:
            sandbox.stop()
        except Exception:
            pass


def pick_sandbox() -> Optional[Sandbox]:
    """ROOTCAUSE_SANDBOX=docker|native|auto (default): prefer containers, fall back to local processes."""
    choice = os.getenv("ROOTCAUSE_SANDBOX", "auto").lower()
    if choice in ("auto", "docker") and DockerComposeSandbox.available():
        return DockerComposeSandbox()
    if choice in ("auto", "native") and NativeSandbox.available():
        return NativeSandbox()
    return None


def plan_and_verify(report: Dict[str, Any], sandbox: Optional[Sandbox] = None) -> Dict[str, Any]:
    proposal = propose(report["diagnosis"])
    if proposal is None:
        return {"status": "no_action", "reason": "no supported diagnosis to remediate"}
    if report["diagnosis"].get("status") != "supported":
        return {"status": "no_action", "proposal": proposal,
                "reason": "diagnosis is inconclusive, so no fix is proposed for execution"}
    if sandbox is None:
        sandbox = pick_sandbox()
        if sandbox is None:
            return {"status": "unverified", "proposal": proposal, "sandbox": "unavailable",
                    "reason": "neither Docker nor the local demo-service dependencies are available, so the fix "
                              "could not be verified in a disposable environment",
                    "production": "proposed, not applied"}
    result = verify(proposal, sandbox)
    return {"status": "verified" if result["verified"] else "failed" if result["verified"] is False else "unverified",
            "proposal": proposal, "sandbox": type(sandbox).__name__, "verification": result,
            "production": "proposed, not applied: apply only after human approval"}
