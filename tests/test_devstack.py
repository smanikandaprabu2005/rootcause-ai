"""Integration: the real services as local processes -> real telemetry -> ingestion -> graph.

Runs the actual FastAPI services with the real OpenTelemetry SDK (no mocks), on offset ports so it cannot
collide with a running stack. Takes about 20 seconds.
"""
import json
import time
import urllib.request

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("uvicorn")

from rootcause import remediation  # noqa: E402
from rootcause.devstack import DevStack  # noqa: E402
from rootcause.graph import build_graph  # noqa: E402
from rootcause.ingest import Ingestor  # noqa: E402
from rootcause.store import open_store  # noqa: E402

OFFSET = 20000


def _call(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    with DevStack(tmp_path_factory.mktemp("stack"), offset=OFFSET, loadgen=False, quiet=True) as st:
        yield st


def test_real_traffic_produces_a_correct_dependency_graph(stack):
    gw = stack.ports["gateway"]
    for i in range(30):
        _call(gw, "POST", "/orders", {"item_id": (i % 20) + 1, "qty": 1})
    _call(gw, "GET", "/products")
    time.sleep(7)                                        # one metric/trace export interval
    store = open_store()
    added = Ingestor(store, stack.telemetry_dir).run_once()
    assert added["traces"] > 0 and added["logs"] > 0 and added["metrics"] > 0
    edges = {(e.caller, e.callee) for e in build_graph(store.spans(0, 2**62), store.services()).values()}
    assert {("gateway", "orders"), ("orders", "inventory"), ("orders", "payments"),
            ("orders", "notifications"), ("gateway", "inventory")} <= edges
    assert {"db.pool.in_use", "process.memory.rss"} <= {m.name for m in store.metrics(0, 2**62)}


def test_injected_fault_changes_real_behaviour_and_remediation_undoes_it(stack):
    gw, pay = stack.ports["gateway"], stack.ports["payments"]

    def order():
        started = time.time()
        try:
            _call(gw, "POST", "/orders", {"item_id": 1, "qty": 1})
            return True, time.time() - started
        except Exception:
            return False, time.time() - started

    assert order()[0]
    _call(pay, "POST", "/admin/fault", {"type": "error", "params": {"rate": 1.0, "status": 500}})
    assert order()[0] is False                           # the failure propagates through orders to the gateway
    _call(pay, "POST", "/admin/remediate", {"action": "clear_error_source"})
    assert order()[0]                                    # and the fix restores service


def test_crashed_service_is_restarted_by_the_supervisor(stack):
    inv = stack.ports["inventory"]
    _call(inv, "POST", "/admin/fault", {"type": "crash", "params": {"after_requests": 0}})
    with pytest.raises(Exception):
        _call(inv, "GET", "/items")                      # the process exits on this request
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            # the fault is persisted, so the restarted process crashes again on its next request: a crash loop
            _call(inv, "GET", "/health")
            break
        except Exception:
            time.sleep(0.3)
    assert stack.svcs["inventory"].restarts >= 1
    stack.svcs["inventory"].next_start = 0.0
    import os
    os.remove(stack.workdir / "state" / "inventory_faults.json")   # stop the loop for the other tests
    time.sleep(2)
    for _ in range(40):
        try:
            _call(inv, "POST", "/admin/fault", {"type": "latency", "params": {"ms": 1}})
            _call(inv, "DELETE", "/admin/fault")
            break
        except Exception:
            time.sleep(0.5)


def test_sandbox_selection_honours_the_environment(monkeypatch):
    monkeypatch.setattr(remediation.DockerComposeSandbox, "available", staticmethod(lambda: False))
    monkeypatch.setenv("ROOTCAUSE_SANDBOX", "docker")
    assert remediation.pick_sandbox() is None            # explicitly asked for Docker, which is unavailable
    monkeypatch.setenv("ROOTCAUSE_SANDBOX", "auto")
    assert isinstance(remediation.pick_sandbox(), remediation.NativeSandbox)
    monkeypatch.setattr(remediation.DockerComposeSandbox, "available", staticmethod(lambda: True))
    assert isinstance(remediation.pick_sandbox(), remediation.DockerComposeSandbox)
