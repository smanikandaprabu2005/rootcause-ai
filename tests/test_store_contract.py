"""One contract, every backend: the same behaviour is required of SQLite and MongoDB.

MongoDB is exercised through mongomock always, and through a real server when ROOTCAUSE_TEST_MONGO is set
(CI does this), e.g. ROOTCAUSE_TEST_MONGO=mongodb://localhost:27017/rc_test
"""
import os
import tempfile
import uuid

import mongomock
import pytest

from rootcause import simulator
from rootcause.config import settings
from rootcause.ingest import Ingestor
from rootcause.investigator import investigate
from rootcause.models import NS, LogRec, MetricPoint, Span
from rootcause.mongo_store import MongoStore
from rootcause.store import SqlStore, open_store

REAL_MONGO = os.getenv("ROOTCAUSE_TEST_MONGO")
BACKENDS = ["sqlite", "mongomock"] + (["mongo"] if REAL_MONGO else [])


@pytest.fixture(params=BACKENDS)
def store(request):
    if request.param == "sqlite":
        yield SqlStore()
    elif request.param == "mongomock":
        yield MongoStore(client=mongomock.MongoClient(), db_name="rc_test")
    else:
        s = MongoStore(REAL_MONGO, db_name=f"rc_{uuid.uuid4().hex[:8]}")  # fresh database per test
        yield s
        s.client.drop_database(s.db.name)


def _span(i, svc="orders", start=1_000, dur=500, parent=None, kind="server", peer=None, status="ok", http=200):
    return Span(f"t{i:030d}", f"s{i:015d}", parent, svc, "GET /x", kind, start, start + dur, status, http, peer,
                {"http.method": "GET", "http.status_code": 200, "$weird": "kept"})


def test_open_store_dispatch_and_rejects_unknown():
    assert isinstance(open_store("sqlite://"), SqlStore)
    with pytest.raises(ValueError):
        open_store("postgresql://x/y")


def test_spans_range_service_filter_order_and_dotted_attrs(store):
    store.add_spans([_span(3, "payments", 3_000), _span(1, "orders", 1_000), _span(2, "orders", 2_000)])
    got = store.spans(0, 10_000)
    assert [s.start_ns for s in got] == [1_000, 2_000, 3_000]                      # ordered by start
    assert [s.service for s in store.spans(0, 10_000, "orders")] == ["orders", "orders"]
    assert [s.start_ns for s in store.spans(1_500, 3_000)] == [2_000]              # [start, end)
    assert got[0].attrs == {"http.method": "GET", "http.status_code": 200, "$weird": "kept"}  # keys round-trip


def test_logs_have_stable_unique_integer_ids_and_lookup(store):
    store.add_logs([LogRec(10, "orders", "ERROR", "boom", "request_error", "t1", "s1", {"upstream": "payments"})])
    store.add_logs([LogRec(20, "orders", "INFO", "ok", None), LogRec(30, "payments", "WARNING", "slow", "x")])
    got = store.logs(0, 100)
    ids = [r.id for r in got]
    assert len(set(ids)) == 3 and all(isinstance(i, int) for i in ids)
    one = store.get_log(ids[0])
    assert one.body == "boom" and one.event == "request_error" and one.attrs == {"upstream": "payments"}
    assert store.get_log(999_999) is None
    assert [r.service for r in store.logs(0, 100, "payments")] == ["payments"]


def test_metrics_filter_by_name(store):
    store.add_metrics([MetricPoint(5, "orders", "process.memory.rss", 1.5e8), MetricPoint(6, "orders", "db.pool.in_use", 3)])
    assert [m.name for m in store.metrics(0, 10)] == ["process.memory.rss", "db.pool.in_use"]
    only = store.metrics(0, 10, "orders", "db.pool.in_use")
    assert len(only) == 1 and only[0].value == 3 and only[0].id is not None


def test_trace_bounds_services_counts(store):
    assert store.time_bounds() is None and store.services() == [] and store.counts() == {"spans": 0, "logs": 0, "metrics": 0}
    a, b = _span(1, "orders", 1_000, 500), _span(1, "payments", 1_200, 100, parent=_span(1).span_id)
    b.span_id = "s" + "9" * 15
    store.add_spans([a, b, _span(2, "gateway", 5_000, 1_000)])
    assert store.time_bounds() == (1_000, 6_000)
    assert store.services() == ["gateway", "orders", "payments"]
    assert [s.service for s in store.get_trace(a.trace_id)] == ["orders", "payments"]
    assert store.counts()["spans"] == 3


def test_offsets_overwrite(store):
    assert store.get_offset("traces.jsonl") == 0
    store.set_offset("traces.jsonl", 100)
    store.set_offset("traces.jsonl", 250)
    assert store.get_offset("traces.jsonl") == 250 and store.get_offset("other") == 0


def test_investigations_round_trip_newest_first(store):
    report = {"id": "a", "diagnosis": {"service": "payments"}, "deviating": {"p95.ms": {"worst": 1.0}}}
    store.save_investigation("a", report)
    store.save_investigation("b", {"id": "b"})
    assert store.get_investigation("a") == report                                  # incl. dotted keys
    assert store.get_investigation("nope") is None
    assert [i["id"] for i in store.list_investigations()] == ["b", "a"]


@pytest.mark.parametrize("scenario,service,fault", [("memory_leak_payments", "payments", "memory_leak"),
                                                    ("slow_notifications_trap", "notifications", "latency")])
def test_whole_pipeline_gives_the_same_diagnosis_on_every_backend(store, scenario, service, fault):
    with tempfile.TemporaryDirectory() as d:
        truth = simulator.generate(scenario, d, seed=21, rps=5.0, fault_s=90.0)
        added = Ingestor(store, d).run_once()
        assert added["traces"] > 0 and Ingestor(store, d).run_once() == {"traces": 0, "metrics": 0, "logs": 0}
    start, end = int(truth["start"] * NS), int(truth["end"] * NS) + 10 * NS
    report = investigate(store, (start, end), (start - int(settings.baseline_seconds * NS), start),
                         use_llm=False, save=True)
    assert (report["diagnosis"]["service"], report["diagnosis"]["fault_type"]) == (service, fault)
    refs = [e["ref"] for e in report["hypotheses"][0]["evidence"] if e["ref"] and e["supports"]]
    log_refs = [r for r in refs if r.startswith("log:") and r[4:].isdigit()]
    trace_refs = [r for r in refs if r.startswith("trace:")]
    assert log_refs or trace_refs                                                  # claims point at raw telemetry
    assert all(store.get_log(int(r[4:])) is not None for r in log_refs)            # ...and those rows exist
    assert all(store.get_trace(r.split("/")[0][len("trace:"):]) for r in trace_refs)
    assert store.get_investigation(report["id"])["diagnosis"]["service"] == service
