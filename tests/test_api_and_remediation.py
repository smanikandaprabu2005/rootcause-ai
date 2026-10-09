"""API contract (what the dashboard consumes) and the remediation agent's honesty rules."""
import tempfile
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from rootcause import remediation, simulator
from rootcause.api import create_app
from rootcause.ingest import Ingestor
from rootcause.models import NS
from rootcause.store import Store


@pytest.fixture(scope="module")
def served():
    with tempfile.TemporaryDirectory() as d:
        truth = simulator.generate("db_pool_orders", d, seed=5, rps=5.0, fault_s=90.0)
        store = Store()
        Ingestor(store, d).run_once()
        yield TestClient(create_app(store=store, telemetry_dir=d)), truth


def test_status_and_services(served):
    client, _ = served
    status = client.get("/api/status").json()
    assert {"gateway", "orders", "inventory", "payments", "notifications"} <= set(status["services"])
    assert status["counts"]["spans"] > 0 and status["counts"]["logs"] > 0 and status["counts"]["metrics"] > 0
    assert client.get("/api/graph").json()["edges"]


def test_investigate_returns_the_shape_the_dashboard_expects(served):
    client, truth = served
    body = {"window_start_ns": int(truth["start"] * NS), "window_end_ns": int((truth["end"] + 10) * NS),
            "use_llm": False}
    report = client.post("/api/investigate", json=body).json()
    for key in ("id", "diagnosis", "anomalies", "graph", "hypotheses", "timeline", "narrative", "narrative_source"):
        assert key in report
    assert report["diagnosis"]["service"] == "orders"
    assert report["diagnosis"]["fault_type"] == "db_pool_exhaustion"
    assert report["narrative_source"] == "evidence"          # no LLM key in tests
    assert client.get(f"/api/investigations/{report['id']}").json()["id"] == report["id"]
    assert any(i["id"] == report["id"] for i in client.get("/api/investigations").json())


def test_evidence_refs_resolve_to_raw_telemetry(served):
    client, truth = served
    body = {"window_start_ns": int(truth["start"] * NS), "window_end_ns": int((truth["end"] + 10) * NS),
            "use_llm": False}
    report = client.post("/api/investigate", json=body).json()
    refs = [e["ref"] for h in report["hypotheses"] for e in h["evidence"] if e["ref"]]
    log_ref = next(r for r in refs if r.startswith("log:") and r[4:].isdigit())
    got = client.get("/api/evidence", params={"ref": log_ref}).json()
    assert got["type"] == "log" and got["record"]["body"]
    trace_ref = next((r for r in refs if r.startswith("trace:")), None)
    if trace_ref:
        spans = client.get("/api/evidence", params={"ref": trace_ref}).json()["spans"]
        assert spans and {"span_id", "parent_id", "duration_ms", "service"} <= set(spans[0])
    assert client.get("/api/evidence", params={"ref": "log:99999999"}).status_code == 404


def test_investigate_without_telemetry_is_a_clean_409():
    client = TestClient(create_app(store=Store(), telemetry_dir=tempfile.mkdtemp()))
    assert client.post("/api/investigate", json={"use_llm": False}).status_code == 409


# --------------------------------------------------------------------------- remediation
class FakeSandbox:
    """Behaves like a stack where the fault degrades health and the action fixes it (or not)."""

    def __init__(self, fix_works: bool = True, reproduces: bool = True):
        self.fix_works, self.reproduces = fix_works, reproduces
        self.state, self.stopped = "healthy", False

    def start(self): pass

    def stop(self): self.stopped = True

    def inject(self, service: str, fault_type: str, params: Dict[str, Any]):
        self.state = "degraded" if self.reproduces else "healthy"

    def remediate(self, service: str, action: str):
        self.state = "healthy" if self.fix_works else "degraded"

    def measure(self):
        return {"error_rate": 0.0, "p95_ms": 20.0} if self.state == "healthy" else {"error_rate": 0.6, "p95_ms": 900.0}


def _report(status="supported", service="payments", fault="memory_leak"):
    return {"diagnosis": {"status": status, "service": service, "fault_type": fault}}


def test_verified_when_fix_recovers_and_sandbox_is_torn_down():
    sb = FakeSandbox(fix_works=True)
    out = remediation.plan_and_verify(_report(), sandbox=sb)
    assert out["status"] == "verified" and sb.stopped
    assert out["production"].startswith("proposed, not applied")  # the agent never touches production


def test_failed_when_fix_does_not_recover():
    assert remediation.plan_and_verify(_report(), sandbox=FakeSandbox(fix_works=False))["status"] == "failed"


def test_unverified_when_failure_cannot_be_reproduced():
    out = remediation.plan_and_verify(_report(), sandbox=FakeSandbox(reproduces=False))
    assert out["status"] == "unverified"
    assert "could not reproduce" in out["verification"]["reason"]


def test_inconclusive_diagnosis_gets_no_executable_fix():
    out = remediation.plan_and_verify(_report(status="inconclusive"), sandbox=FakeSandbox())
    assert out["status"] == "no_action"


def test_every_fault_type_has_a_playbook_and_repro():
    from rootcause.hypotheses import FAULT_TYPES

    assert set(FAULT_TYPES) <= set(remediation.PLAYBOOK) and set(FAULT_TYPES) <= set(remediation.REPRO_PARAMS)
