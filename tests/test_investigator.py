"""End-to-end tests of the investigator on simulated telemetry (no Docker, no network, no LLM)."""
import tempfile

import numpy as np
import pytest

from rootcause import simulator
from rootcause.anomaly import detect
from rootcause.config import settings
from rootcause.features import FEATURES
from rootcause.ingest import Ingestor
from rootcause.investigator import investigate
from rootcause.models import NS
from rootcause.store import open_store


def _run(scenario: str, seed: int = 11, rps: float = 5.0, fault_s: float = 90.0):
    with tempfile.TemporaryDirectory() as d:
        truth = simulator.generate(scenario, d, seed=seed, rps=rps, fault_s=fault_s)
        store = open_store()
        Ingestor(store, d).run_once()
    start, end = int(truth["start"] * NS), int(truth["end"] * NS)
    window = (start, end + 10 * NS)
    baseline = (start - int(settings.baseline_seconds * NS), start)
    return truth, store, investigate(store, window, baseline, use_llm=False)


@pytest.mark.parametrize("scenario", [
    "latency_payments", "crash_inventory", "db_pool_orders", "memory_leak_payments",
    "bad_deploy_orders", "error_burst_inventory", "slow_notifications_trap", "db_pool_payments",
])
def test_diagnoses_each_scenario(scenario):
    truth, _, report = _run(scenario)
    d = report["diagnosis"]
    assert d["status"] == "supported"
    assert (d["service"], d["fault_type"]) == (truth["root_cause_service"], truth["fault_type"])


def test_healthy_control_reports_no_incident():
    _, _, report = _run("no_fault_control")
    assert report["diagnosis"]["status"] == "no_incident"


def test_slow_notifications_trap_does_not_blame_orders():
    """Orders looks slow only because it waits on notifications; the diagnosis must follow the evidence."""
    _, _, report = _run("slow_notifications_trap")
    assert report["diagnosis"]["service"] == "notifications"
    orders = [h for h in report["hypotheses"] if h["service"] == "orders" and h["fault_type"] == "latency"]
    assert orders and all(h["status"] == "rejected" for h in orders)


def test_every_supporting_claim_points_at_raw_telemetry():
    _, store, report = _run("db_pool_orders")
    best = next(h for h in report["hypotheses"] if h["status"] != "rejected")
    supporting = [e for e in best["evidence"] if e["supports"]]
    assert supporting and all(e["ref"] for e in supporting)
    for e in supporting:
        if e["ref"].startswith("log:") and e["ref"][4:].isdigit():
            assert store.get_log(int(e["ref"][4:])) is not None


def test_report_never_leaks_the_injected_fault_name():
    """The investigator must work from symptoms: simulator labels must not appear in telemetry."""
    with tempfile.TemporaryDirectory() as d:
        simulator.generate("db_pool_payments", d, seed=3, rps=4.0, fault_s=60.0)
        store = open_store()
        Ingestor(store, d).run_once()
    assert not any("fault_activated" in (r.event or "") for r in store.logs(0, 2**62))


def test_timeline_is_ordered_and_referenced():
    _, _, report = _run("bad_deploy_orders")
    ts = [e["ts_ns"] for e in report["timeline"]]
    assert ts == sorted(ts) and report["timeline"]
    assert all(e["ref"] for e in report["timeline"])


def test_forest_detects_deviation_on_a_constant_baseline_feature():
    """Regression: error_rate/inbound_fail are exactly 0 when healthy. An isolation forest cannot split a
    constant axis, so without a noise floor in its training data it never isolates such a deviation."""
    base_rows = np.zeros((40, len(FEATURES)))
    base_rows[:, 0] = np.random.default_rng(0).normal(20, 1, 40)   # requests: some natural variation
    base_rows[:, 2] = base_rows[:, 3] = 12.0                       # latency flat
    base_rows[:, 7] = 13.0

    # Build spans/metrics-free matrices by calling the detector's internals through bucketize substitutes.
    import rootcause.anomaly as anomaly

    original = anomaly.bucketize
    bucket = 5 * NS
    win_rows = base_rows[:6].copy()
    win_rows[:, 6] = 0.85  # callers suddenly see 85% failures
    anomaly.bucketize = lambda spans, metrics, s, e, b, services: (
        {"svc": base_rows} if s == 0 else {"svc": win_rows})
    try:
        found = detect([], [], (0, 40 * bucket), (40 * bucket, 46 * bucket), ["svc"], bucket)
    finally:
        anomaly.bucketize = original
    assert [a.service for a in found] == ["svc"]
    assert "inbound_fail" in found[0].deviating


def test_llm_narrative_is_rejected_unless_it_cites_real_evidence(monkeypatch):
    from rootcause import llm

    refs = {"log:1", "trace:ab/span:cd"}
    assert llm.citations_are_valid("Pool exhausted [log:1, trace:ab/span:cd].", refs)
    assert not llm.citations_are_valid("Pool exhausted, no sources.", refs)
    assert not llm.citations_are_valid("Caused by a hacker [log:999].", refs)   # invented ref

    _, _, report = _run("db_pool_orders")
    monkeypatch.setattr(llm, "generate", lambda prompt: "Orders is broken because of cosmic rays.")
    assert llm.narrate(report) is None   # uncited prose never replaces the evidence-built summary


# ----------------------------------------------------------------------------- incident discovery
from rootcause.incident import find_incident  # noqa: E402


def _store_for(scenario: str, seed: int = 31):
    with tempfile.TemporaryDirectory() as d:
        truth = simulator.generate(scenario, d, seed=seed, rps=5.0, fault_s=90.0)
        store = open_store()
        Ingestor(store, d).run_once()
    return truth, store


@pytest.mark.parametrize("scenario", ["latency_payments", "crash_inventory", "db_pool_orders", "memory_leak_payments",
                                      "bad_deploy_orders", "error_burst_inventory", "slow_notifications_trap",
                                      "db_pool_payments"])
def test_default_investigation_finds_an_incident_that_is_still_under_way(scenario):
    """Regression: with a fixed 'last 90 s' window the fault sat inside its own baseline and every hypothesis
    was falsified. 45 s into the incident, no window is given and the answer must still be right."""
    truth, store = _store_for(scenario)
    as_of = int((truth["start"] + 45) * NS)
    found = find_incident(store, as_of)
    assert found.found and found.ongoing
    assert found.window[0] <= int((truth["start"] + 5) * NS)                 # onset is inside the window
    assert found.baseline[1] <= found.window[0] + 1                          # baseline ends where the window begins
    report = investigate(store, use_llm=False, as_of_ns=as_of)
    assert report["window_source"] == "discovered"
    d = report["diagnosis"]
    assert (d["status"], d["service"], d["fault_type"]) == ("supported", truth["root_cause_service"], truth["fault_type"])


def test_discovery_after_recovery_and_on_a_healthy_system():
    truth, store = _store_for("slow_notifications_trap")
    after = investigate(store, use_llm=False, as_of_ns=int((truth["end"] + 30) * NS))
    assert after["diagnosis"]["service"] == "notifications" and after["discovery"]["found"]

    _, healthy = _store_for("no_fault_control")
    found = find_incident(healthy)
    assert not found.found
    assert investigate(healthy, use_llm=False)["diagnosis"]["status"] == "no_incident"


def test_explicit_window_is_still_honoured():
    truth, store = _store_for("db_pool_orders")
    start, end = int(truth["start"] * NS), int(truth["end"] * NS) + 10 * NS
    report = investigate(store, (start, end), use_llm=False)
    assert report["window_source"] == "given" and "discovery" not in report
    assert report["window"]["start_ns"] == start


def test_sustained_errors_are_found_even_when_the_baseline_has_its_own_blips():
    """Regression from the first live run: ~8% of healthy buckets had 25% errors, which lowered the forest's
    threshold until a sustained 50% error rate looked ordinary and bad_deploy went undetected."""
    import rootcause.anomaly as anomaly

    rng = np.random.default_rng(1)
    base = np.zeros((24, len(FEATURES)))
    base[:, 0] = rng.normal(21, 2, 24)
    base[:, 2] = rng.normal(58, 6, 24)
    base[:, 3] = rng.normal(11, 2, 24)
    base[:, 4] = 69.3
    base[:, 7] = base[:, 2] + 3
    base[[2, 4, 6, 13, 16], 1] = 0.25          # blips
    base[[2, 4, 6, 13, 16], 6] = 0.25
    win = base[:12].copy()
    win[:, 1] = win[:, 6] = rng.uniform(0.4, 0.7, 12)   # sustained fault
    original = anomaly.bucketize
    anomaly.bucketize = lambda sp, me, s, e, b, svcs: {"svc": base} if s == 0 else {"svc": win}
    try:
        found = detect([], [], (0, 24 * 5 * NS), (24 * 5 * NS, 36 * 5 * NS), ["svc"], 5 * NS)
    finally:
        anomaly.bucketize = original
    assert [a.service for a in found] == ["svc"] and found[0].anomalous_buckets >= 10
