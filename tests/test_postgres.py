"""Runs the pipeline against a real PostgreSQL. Skipped unless ROOTCAUSE_TEST_PG is set, e.g.

    ROOTCAUSE_TEST_PG=postgresql+psycopg2://user:pass@localhost:5432/rc_test pytest tests/test_postgres.py

CI sets it from a Postgres service container.
"""
import os
import tempfile

import pytest

from rootcause import simulator
from rootcause.config import settings
from rootcause.ingest import Ingestor
from rootcause.investigator import investigate
from rootcause.models import NS
from rootcause.store import Store

PG = os.getenv("ROOTCAUSE_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="set ROOTCAUSE_TEST_PG to run against PostgreSQL")


def test_ingest_is_idempotent_and_diagnosis_matches_on_postgres():
    with tempfile.TemporaryDirectory() as d:
        truth = simulator.generate("memory_leak_payments", d, seed=21, rps=5.0, fault_s=90.0)
        store = Store(PG)
        first = Ingestor(store, d).run_once()
        assert first["traces"] > 0
        assert Ingestor(store, d).run_once() == {"traces": 0, "metrics": 0, "logs": 0}  # offsets prevent re-ingest
        start, end = int(truth["start"] * NS), int(truth["end"] * NS) + 10 * NS
        report = investigate(store, (start, end), (start - int(settings.baseline_seconds * NS), start),
                             use_llm=False, save=True)
        assert (report["diagnosis"]["service"], report["diagnosis"]["fault_type"]) == ("payments", "memory_leak")
        assert store.get_investigation(report["id"])["id"] == report["id"]
