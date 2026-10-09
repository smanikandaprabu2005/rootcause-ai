"""Unit tests for the fault-injection layer (no Docker or Postgres needed)."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from fastapi.testclient import TestClient  # noqa: E402

from common.base import FAULT_TYPES, make_app  # noqa: E402


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULT_STATE_FILE", str(tmp_path / "faults.json"))
    c = make_app("testsvc")

    @c.app.get("/ping")
    def ping():
        return {"pong": True}

    return c


@pytest.fixture()
def client(ctx):
    return TestClient(ctx.app)


def test_healthy_by_default(client):
    assert client.get("/ping").json() == {"pong": True}
    assert client.get("/admin/fault").json()["active"] == {}


def test_latency_fault_slows_requests_and_clears(client):
    client.post("/admin/fault", json={"type": "latency", "params": {"ms": 300}})
    started = time.time()
    client.get("/ping")
    assert time.time() - started >= 0.3
    client.delete("/admin/fault")
    started = time.time()
    client.get("/ping")
    assert time.time() - started < 0.2


def test_error_fault_returns_configured_status(client):
    client.post("/admin/fault", json={"type": "error", "params": {"rate": 1.0, "status": 503}})
    assert client.get("/ping").status_code == 503
    client.delete("/admin/fault")
    assert client.get("/ping").status_code == 200


def test_admin_and_health_paths_are_never_faulted(client):
    client.post("/admin/fault", json={"type": "error", "params": {"rate": 1.0}})
    assert client.get("/health").status_code == 200
    assert client.get("/admin/fault").status_code == 200


def test_bad_deploy_changes_version_header_and_errors(client, ctx):
    client.post("/admin/fault", json={"type": "bad_deploy", "params": {"version": "v9", "error_rate": 1.0}})
    resp = client.get("/ping")
    assert resp.status_code == 500
    assert resp.headers["x-service-version"] == "v9"
    client.delete("/admin/fault")
    assert client.get("/ping").headers["x-service-version"] == ctx.version


def test_memory_leak_grows_and_clear_releases(client, ctx):
    client.post("/admin/fault", json={"type": "memory_leak", "params": {"mb_per_request": 1}})
    for _ in range(3):
        client.get("/ping")
    assert len(ctx.faults._leak) == 3
    client.delete("/admin/fault")
    assert ctx.faults._leak == []


def test_unknown_fault_rejected(client):
    assert client.post("/admin/fault", json={"type": "gremlins"}).status_code == 400


def test_db_fault_rejected_without_database(client):
    assert client.post("/admin/fault", json={"type": "db_pool_exhaustion"}).status_code == 400


def test_scenarios_only_use_known_fault_types():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "faults"))
    import inject

    for name, sc in inject.SCENARIOS.items():
        assert sc["type"] in FAULT_TYPES, name
        assert sc["service"] in inject.SERVICE_URLS, name


def test_remediation_clears_matching_fault(client):
    client.post("/admin/fault", json={"type": "bad_deploy", "params": {"version": "v9", "error_rate": 1.0}})
    assert client.get("/ping").status_code == 500
    out = client.post("/admin/remediate", json={"action": "rollback_deploy"}).json()
    assert out["cleared"] == "bad_deploy"
    assert client.get("/ping").status_code == 200


def test_unknown_remediation_rejected(client):
    assert client.post("/admin/remediate", json={"action": "reboot_the_internet"}).status_code == 400


def test_log_extras_are_flat_json_fields():
    import json
    import logging

    from common.base import JsonFormatter

    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, None)
    rec.event = "db_pool_exhausted"
    rec.upstream = "payments"
    out = json.loads(JsonFormatter("svc").format(rec))
    assert out["event"] == "db_pool_exhausted" and out["upstream"] == "payments"
    assert out["service"] == "svc" and "fields" not in out


def test_faults_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULT_STATE_FILE", str(tmp_path / "faults.json"))
    first = make_app("testsvc")
    first.faults.apply("error", {"rate": 1.0})
    restarted = make_app("testsvc")  # new process, same container filesystem
    assert "error" in restarted.faults.snapshot()
    restarted.faults.clear()
    assert "error" not in make_app("testsvc").faults.snapshot()
