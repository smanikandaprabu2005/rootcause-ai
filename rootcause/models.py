"""Plain data types shared by every stage of the investigator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

NS = 1_000_000_000


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    service: str
    name: str
    kind: str  # "server" | "client" | "internal"
    start_ns: int
    end_ns: int
    status: str  # "ok" | "error" | "unset"
    http_status: Optional[int] = None
    peer: Optional[str] = None  # client spans: host of the called service
    attrs: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1e6

    @property
    def is_error(self) -> bool:
        return self.status == "error" or (self.http_status is not None and self.http_status >= 500)


@dataclass
class LogRec:
    ts_ns: int
    service: str
    severity: str
    body: str
    event: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None  # database id, used as an evidence reference


@dataclass
class MetricPoint:
    ts_ns: int
    service: str
    name: str
    value: float
    attrs: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None


@dataclass
class Evidence:
    """One verifiable fact, pointing at the raw telemetry it came from."""

    kind: str  # "log" | "span" | "metric" | "graph" | "stat"
    service: str
    summary: str
    ts_ns: Optional[int] = None
    ref: Optional[str] = None  # e.g. "log:123", "trace:abcd/span:ef01", "metric:rss@payments"
    supports: bool = True  # False = this evidence contradicts the hypothesis
    weight: float = 1.0    # how strongly it counts, either way
    decisive: bool = False  # a decisive contradiction falsifies the hypothesis outright

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "service": self.service, "summary": self.summary,
            "ts_ns": self.ts_ns, "ref": self.ref, "supports": self.supports,
            "weight": self.weight, "decisive": self.decisive,
        }


@dataclass
class Hypothesis:
    service: str
    fault_type: str  # latency | crash | db_pool_exhaustion | memory_leak | bad_deploy | error
    prior: float = 0.0
    evidence: List[Evidence] = field(default_factory=list)
    score: float = 0.0
    confidence: float = 0.0
    status: str = "open"  # open | supported | rejected
    rejected_because: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service, "fault_type": self.fault_type, "score": round(self.score, 4),
            "confidence": round(self.confidence, 4), "status": self.status,
            "rejected_because": self.rejected_because,
            "evidence": [e.to_dict() for e in self.evidence],
        }
