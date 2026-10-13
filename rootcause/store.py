"""Telemetry storage behind one interface (``Store``).

* ``MongoStore`` (rootcause/mongo_store.py): production. Spans, logs and metrics are variable-shape documents and
  investigation reports are nested JSON, which is what a document database is for.
* ``SqlStore``: embedded SQLite, zero configuration, used for tests, the offline benchmark and Docker-free runs.

Pick one with ``open_store(url)``: ``mongodb://...`` or ``sqlite:///file.db`` (``sqlite://`` is in-memory).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence

from sqlalchemy import (
    JSON, BigInteger, Column, Float, Index, Integer, MetaData, String, Table, Text, create_engine, func, insert, select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from .models import LogRec, MetricPoint, Span

metadata = MetaData()

spans_t = Table(
    "spans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trace_id", String(32), nullable=False), Column("span_id", String(16), nullable=False),
    Column("parent_id", String(16)), Column("service", String(64), nullable=False),
    Column("name", Text), Column("kind", String(16)),
    Column("start_ns", BigInteger, nullable=False), Column("end_ns", BigInteger, nullable=False),
    Column("status", String(8)), Column("http_status", Integer), Column("peer", String(64)),
    Column("attrs", JSON),
    Index("ix_spans_start", "start_ns"), Index("ix_spans_trace", "trace_id"),
)
logs_t = Table(
    "logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts_ns", BigInteger, nullable=False), Column("service", String(64), nullable=False),
    Column("severity", String(16)), Column("body", Text), Column("event", String(64)),
    Column("trace_id", String(32)), Column("span_id", String(16)), Column("attrs", JSON),
    Index("ix_logs_ts", "ts_ns"),
)
metrics_t = Table(
    "metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts_ns", BigInteger, nullable=False), Column("service", String(64), nullable=False),
    Column("name", String(128), nullable=False), Column("value", Float), Column("attrs", JSON),
    Index("ix_metrics_ts", "ts_ns"),
)
ingest_state_t = Table(
    "ingest_state", metadata,
    Column("source", String(256), primary_key=True), Column("offset", BigInteger, nullable=False),
)
investigations_t = Table(
    "investigations", metadata,
    Column("id", String(32), primary_key=True), Column("created_at", Float, nullable=False),
    Column("report", JSON, nullable=False),
)

_BATCH = 2000


def _chunks(rows: Sequence[dict], n: int = _BATCH) -> Iterable[Sequence[dict]]:
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


class Store(Protocol):
    """What the investigator, ingestor and API need from storage."""

    def add_spans(self, spans: Sequence[Span]) -> None: ...
    def add_logs(self, logs: Sequence[LogRec]) -> None: ...
    def add_metrics(self, points: Sequence[MetricPoint]) -> None: ...
    def get_offset(self, source: str) -> int: ...
    def set_offset(self, source: str, offset: int) -> None: ...
    def spans(self, start_ns: int, end_ns: int, service: Optional[str] = None) -> List[Span]: ...
    def logs(self, start_ns: int, end_ns: int, service: Optional[str] = None) -> List[LogRec]: ...
    def metrics(self, start_ns: int, end_ns: int, service: Optional[str] = None,
                name: Optional[str] = None) -> List[MetricPoint]: ...
    def get_log(self, log_id: int) -> Optional[LogRec]: ...
    def get_trace(self, trace_id: str) -> List[Span]: ...
    def time_bounds(self) -> Optional[tuple]: ...
    def services(self) -> List[str]: ...
    def counts(self) -> Dict[str, int]: ...
    def save_investigation(self, inv_id: str, report: Dict[str, Any]) -> None: ...
    def get_investigation(self, inv_id: str) -> Optional[Dict[str, Any]]: ...
    def list_investigations(self, limit: int = 50) -> List[Dict[str, Any]]: ...


def open_store(url: str = "sqlite://") -> Store:
    """``mongodb://host:27017/rootcause`` -> MongoStore; ``sqlite://`` or ``sqlite:///path`` -> SqlStore."""
    if url.startswith(("mongodb://", "mongodb+srv://")):
        from .mongo_store import MongoStore

        return MongoStore(url)
    if url.startswith("sqlite"):
        return SqlStore(url)
    raise ValueError(f"unsupported store URL {url.split('://')[0]}://; use mongodb:// or sqlite://")


class SqlStore:
    """Embedded SQLite implementation of ``Store``."""

    def __init__(self, url: str = "sqlite://"):
        if not url.startswith("sqlite"):
            raise ValueError("SqlStore only supports sqlite URLs")
        kw: Dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw["poolclass"] = StaticPool  # one shared in-memory database
        self.engine: Engine = create_engine(url, **kw)
        metadata.create_all(self.engine)

    # ---- writes -------------------------------------------------------------
    def add_spans(self, spans: Sequence[Span]) -> None:
        rows = [
            dict(trace_id=s.trace_id, span_id=s.span_id, parent_id=s.parent_id, service=s.service, name=s.name,
                 kind=s.kind, start_ns=s.start_ns, end_ns=s.end_ns, status=s.status, http_status=s.http_status,
                 peer=s.peer, attrs=s.attrs)
            for s in spans
        ]
        with self.engine.begin() as c:
            for chunk in _chunks(rows):
                c.execute(insert(spans_t), list(chunk))

    def add_logs(self, logs: Sequence[LogRec]) -> None:
        rows = [
            dict(ts_ns=r.ts_ns, service=r.service, severity=r.severity, body=r.body, event=r.event,
                 trace_id=r.trace_id, span_id=r.span_id, attrs=r.attrs)
            for r in logs
        ]
        with self.engine.begin() as c:
            for chunk in _chunks(rows):
                c.execute(insert(logs_t), list(chunk))

    def add_metrics(self, points: Sequence[MetricPoint]) -> None:
        rows = [dict(ts_ns=p.ts_ns, service=p.service, name=p.name, value=p.value, attrs=p.attrs) for p in points]
        with self.engine.begin() as c:
            for chunk in _chunks(rows):
                c.execute(insert(metrics_t), list(chunk))

    def get_offset(self, source: str) -> int:
        with self.engine.begin() as c:
            row = c.execute(select(ingest_state_t.c.offset).where(ingest_state_t.c.source == source)).first()
        return int(row[0]) if row else 0

    def set_offset(self, source: str, offset: int) -> None:
        with self.engine.begin() as c:
            updated = c.execute(
                ingest_state_t.update().where(ingest_state_t.c.source == source).values(offset=offset)
            ).rowcount
            if not updated:
                c.execute(insert(ingest_state_t).values(source=source, offset=offset))

    # ---- reads --------------------------------------------------------------
    def spans(self, start_ns: int, end_ns: int, service: Optional[str] = None) -> List[Span]:
        q = select(spans_t).where(spans_t.c.start_ns >= start_ns, spans_t.c.start_ns < end_ns)
        if service:
            q = q.where(spans_t.c.service == service)
        with self.engine.begin() as c:
            rows = c.execute(q.order_by(spans_t.c.start_ns)).mappings().all()
        return [
            Span(r["trace_id"], r["span_id"], r["parent_id"], r["service"], r["name"] or "", r["kind"] or "internal",
                 r["start_ns"], r["end_ns"], r["status"] or "unset", r["http_status"], r["peer"], r["attrs"] or {})
            for r in rows
        ]

    def logs(self, start_ns: int, end_ns: int, service: Optional[str] = None) -> List[LogRec]:
        q = select(logs_t).where(logs_t.c.ts_ns >= start_ns, logs_t.c.ts_ns < end_ns)
        if service:
            q = q.where(logs_t.c.service == service)
        with self.engine.begin() as c:
            rows = c.execute(q.order_by(logs_t.c.ts_ns)).mappings().all()
        return [
            LogRec(r["ts_ns"], r["service"], r["severity"] or "INFO", r["body"] or "", r["event"], r["trace_id"],
                   r["span_id"], r["attrs"] or {}, id=r["id"])
            for r in rows
        ]

    def metrics(self, start_ns: int, end_ns: int, service: Optional[str] = None,
                name: Optional[str] = None) -> List[MetricPoint]:
        q = select(metrics_t).where(metrics_t.c.ts_ns >= start_ns, metrics_t.c.ts_ns < end_ns)
        if service:
            q = q.where(metrics_t.c.service == service)
        if name:
            q = q.where(metrics_t.c.name == name)
        with self.engine.begin() as c:
            rows = c.execute(q.order_by(metrics_t.c.ts_ns)).mappings().all()
        return [
            MetricPoint(r["ts_ns"], r["service"], r["name"], r["value"] or 0.0, r["attrs"] or {}, id=r["id"])
            for r in rows
        ]

    def get_log(self, log_id: int) -> Optional[LogRec]:
        with self.engine.begin() as c:
            r = c.execute(select(logs_t).where(logs_t.c.id == log_id)).mappings().first()
        if not r:
            return None
        return LogRec(r["ts_ns"], r["service"], r["severity"] or "INFO", r["body"] or "", r["event"], r["trace_id"],
                      r["span_id"], r["attrs"] or {}, id=r["id"])

    def get_trace(self, trace_id: str) -> List[Span]:
        with self.engine.begin() as c:
            rows = c.execute(select(spans_t).where(spans_t.c.trace_id == trace_id)
                             .order_by(spans_t.c.start_ns)).mappings().all()
        return [
            Span(r["trace_id"], r["span_id"], r["parent_id"], r["service"], r["name"] or "", r["kind"] or "internal",
                 r["start_ns"], r["end_ns"], r["status"] or "unset", r["http_status"], r["peer"], r["attrs"] or {})
            for r in rows
        ]

    def time_bounds(self) -> Optional[tuple]:
        with self.engine.begin() as c:
            lo, hi = c.execute(select(func.min(spans_t.c.start_ns), func.max(spans_t.c.end_ns))).first()
        return (int(lo), int(hi)) if lo is not None else None

    def services(self) -> List[str]:
        with self.engine.begin() as c:
            rows = c.execute(select(spans_t.c.service).distinct()).all()
        return sorted(r[0] for r in rows)

    def counts(self) -> Dict[str, int]:
        with self.engine.begin() as c:
            return {
                "spans": c.execute(select(func.count()).select_from(spans_t)).scalar_one(),
                "logs": c.execute(select(func.count()).select_from(logs_t)).scalar_one(),
                "metrics": c.execute(select(func.count()).select_from(metrics_t)).scalar_one(),
            }

    # ---- investigations -----------------------------------------------------
    def save_investigation(self, inv_id: str, report: Dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(insert(investigations_t).values(id=inv_id, created_at=time.time(), report=report))

    def get_investigation(self, inv_id: str) -> Optional[Dict[str, Any]]:
        with self.engine.begin() as c:
            row = c.execute(select(investigations_t.c.report).where(investigations_t.c.id == inv_id)).first()
        return row[0] if row else None

    def list_investigations(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.engine.begin() as c:
            rows = c.execute(
                select(investigations_t.c.id, investigations_t.c.created_at)
                .order_by(investigations_t.c.created_at.desc()).limit(limit)
            ).all()
        return [{"id": r[0], "created_at": r[1]} for r in rows]
