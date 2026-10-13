"""MongoDB implementation of ``Store``.

Collections
  spans           one document per span; indexed on start_ns, end_ns, trace_id and (service, start_ns)
  logs, metrics   one document per record, each with a stable integer ``id`` (evidence refs look like ``log:123``)
  ingest_state    {_id: <file>, offset}: how far each collector file has been read
  investigations  {_id: <id>, created_at, report}: the full nested report, stored as-is
  counters        {_id: "logs" | "metrics", n}: sources of the integer ids

Why a document store here: telemetry attributes vary per span and per log line, and a report is a deeply
nested document that is always read and written whole. Neither benefits from a relational schema.

BSON field names must not contain "." or start with "$", but OpenTelemetry attribute names are full of dots
(``http.status_code``). Keys are therefore encoded on write and decoded on read, so callers never see it.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

from .models import LogRec, MetricPoint, Span

_BATCH = 2000
_DOT, _DOLLAR = "．", "＄"  # fullwidth look-alikes, the usual encoding for these characters


def _enc(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k).replace(".", _DOT).replace("$", _DOLLAR): _enc(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_enc(v) for v in value]
    return value


def _dec(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k).replace(_DOT, ".").replace(_DOLLAR, "$"): _dec(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dec(v) for v in value]
    return value


def _chunks(rows: Sequence[dict], n: int = _BATCH) -> Iterable[Sequence[dict]]:
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def _span(d: Dict[str, Any]) -> Span:
    return Span(d["trace_id"], d["span_id"], d.get("parent_id"), d["service"], d.get("name") or "",
                d.get("kind") or "internal", d["start_ns"], d["end_ns"], d.get("status") or "unset",
                d.get("http_status"), d.get("peer"), _dec(d.get("attrs") or {}))


def _log(d: Dict[str, Any]) -> LogRec:
    return LogRec(d["ts_ns"], d["service"], d.get("severity") or "INFO", d.get("body") or "", d.get("event"),
                  d.get("trace_id"), d.get("span_id"), _dec(d.get("attrs") or {}), id=d["id"])


class MongoStore:
    def __init__(self, url: str = "mongodb://localhost:27017/rootcause", client: Any = None,
                 db_name: Optional[str] = None):
        """``client`` lets tests inject a ``mongomock.MongoClient``."""
        if client is None:
            from pymongo import MongoClient

            client = MongoClient(url, serverSelectionTimeoutMS=8000, tz_aware=True)
        path = urlparse(url).path.strip("/")
        self.db = client[db_name or path or "rootcause"]
        self.client = client
        self.db.spans.create_index("start_ns")
        self.db.spans.create_index("end_ns")
        self.db.spans.create_index("trace_id")
        self.db.spans.create_index([("service", 1), ("start_ns", 1)])
        self.db.logs.create_index("id", unique=True)
        self.db.logs.create_index("ts_ns")
        self.db.metrics.create_index("id", unique=True)
        self.db.metrics.create_index("ts_ns")
        self.db.investigations.create_index([("created_at", -1)])

    # ---- writes -------------------------------------------------------------
    def _reserve_ids(self, name: str, n: int) -> int:
        """Atomically reserve ``n`` consecutive integer ids; returns the first."""
        from pymongo import ReturnDocument

        doc = self.db.counters.find_one_and_update(
            {"_id": name}, {"$inc": {"n": n}}, upsert=True, return_document=ReturnDocument.AFTER)
        return int(doc["n"]) - n + 1

    def add_spans(self, spans: Sequence[Span]) -> None:
        rows = [dict(trace_id=s.trace_id, span_id=s.span_id, parent_id=s.parent_id, service=s.service, name=s.name,
                     kind=s.kind, start_ns=s.start_ns, end_ns=s.end_ns, status=s.status, http_status=s.http_status,
                     peer=s.peer, attrs=_enc(s.attrs)) for s in spans]
        for chunk in _chunks(rows):
            self.db.spans.insert_many(list(chunk), ordered=False)

    def add_logs(self, logs: Sequence[LogRec]) -> None:
        if not logs:
            return
        first = self._reserve_ids("logs", len(logs))
        rows = [dict(id=first + i, ts_ns=r.ts_ns, service=r.service, severity=r.severity, body=r.body, event=r.event,
                     trace_id=r.trace_id, span_id=r.span_id, attrs=_enc(r.attrs)) for i, r in enumerate(logs)]
        for chunk in _chunks(rows):
            self.db.logs.insert_many(list(chunk), ordered=False)

    def add_metrics(self, points: Sequence[MetricPoint]) -> None:
        if not points:
            return
        first = self._reserve_ids("metrics", len(points))
        rows = [dict(id=first + i, ts_ns=p.ts_ns, service=p.service, name=p.name, value=p.value, attrs=_enc(p.attrs))
                for i, p in enumerate(points)]
        for chunk in _chunks(rows):
            self.db.metrics.insert_many(list(chunk), ordered=False)

    def get_offset(self, source: str) -> int:
        doc = self.db.ingest_state.find_one({"_id": source})
        return int(doc["offset"]) if doc else 0

    def set_offset(self, source: str, offset: int) -> None:
        self.db.ingest_state.update_one({"_id": source}, {"$set": {"offset": int(offset)}}, upsert=True)

    # ---- reads --------------------------------------------------------------
    @staticmethod
    def _range(field: str, start_ns: int, end_ns: int, service: Optional[str]) -> Dict[str, Any]:
        q: Dict[str, Any] = {field: {"$gte": start_ns, "$lt": end_ns}}
        if service:
            q["service"] = service
        return q

    def spans(self, start_ns: int, end_ns: int, service: Optional[str] = None) -> List[Span]:
        cur = self.db.spans.find(self._range("start_ns", start_ns, end_ns, service), {"_id": 0}).sort("start_ns", 1)
        return [_span(d) for d in cur]

    def logs(self, start_ns: int, end_ns: int, service: Optional[str] = None) -> List[LogRec]:
        cur = self.db.logs.find(self._range("ts_ns", start_ns, end_ns, service), {"_id": 0}).sort(
            [("ts_ns", 1), ("id", 1)])
        return [_log(d) for d in cur]

    def metrics(self, start_ns: int, end_ns: int, service: Optional[str] = None,
                name: Optional[str] = None) -> List[MetricPoint]:
        q = self._range("ts_ns", start_ns, end_ns, service)
        if name:
            q["name"] = name
        cur = self.db.metrics.find(q, {"_id": 0}).sort([("ts_ns", 1), ("id", 1)])
        return [MetricPoint(d["ts_ns"], d["service"], d["name"], d.get("value") or 0.0, _dec(d.get("attrs") or {}),
                            id=d["id"]) for d in cur]

    def get_log(self, log_id: int) -> Optional[LogRec]:
        d = self.db.logs.find_one({"id": int(log_id)}, {"_id": 0})
        return _log(d) if d else None

    def get_trace(self, trace_id: str) -> List[Span]:
        cur = self.db.spans.find({"trace_id": trace_id}, {"_id": 0}).sort("start_ns", 1)
        return [_span(d) for d in cur]

    def time_bounds(self) -> Optional[tuple]:
        lo = next(iter(self.db.spans.find({}, {"start_ns": 1}).sort("start_ns", 1).limit(1)), None)
        if lo is None:
            return None
        hi = next(iter(self.db.spans.find({}, {"end_ns": 1}).sort("end_ns", -1).limit(1)))
        return int(lo["start_ns"]), int(hi["end_ns"])

    def services(self) -> List[str]:
        return sorted(self.db.spans.distinct("service"))

    def counts(self) -> Dict[str, int]:
        return {"spans": self.db.spans.count_documents({}), "logs": self.db.logs.count_documents({}),
                "metrics": self.db.metrics.count_documents({})}

    # ---- investigations -----------------------------------------------------
    def save_investigation(self, inv_id: str, report: Dict[str, Any]) -> None:
        self.db.investigations.replace_one({"_id": inv_id}, {"_id": inv_id, "created_at": time.time(),
                                                             "report": _enc(report)}, upsert=True)

    def get_investigation(self, inv_id: str) -> Optional[Dict[str, Any]]:
        d = self.db.investigations.find_one({"_id": inv_id})
        return _dec(d["report"]) if d else None

    def list_investigations(self, limit: int = 50) -> List[Dict[str, Any]]:
        cur = self.db.investigations.find({}, {"created_at": 1}).sort("created_at", -1).limit(limit)
        return [{"id": d["_id"], "created_at": d["created_at"]} for d in cur]
