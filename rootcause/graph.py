"""Service dependency graph, derived from traces, plus failure-propagation reasoning."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Sequence, Set, Tuple

from .features import percentile
from .models import Span


@dataclass
class Edge:
    caller: str
    callee: str
    calls: int
    errors: int
    p95_ms: float

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0

    def to_dict(self) -> dict:
        return {"caller": self.caller, "callee": self.callee, "calls": self.calls, "errors": self.errors,
                "error_rate": round(self.error_rate, 4), "p95_ms": round(self.p95_ms, 1)}


def build_graph(spans: Sequence[Span], services: Sequence[str]) -> Dict[Tuple[str, str], Edge]:
    known: Set[str] = set(services)
    server_of_client: Dict[str, str] = {s.parent_id: s.service for s in spans if s.kind == "server" and s.parent_id}
    durations: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    errors: Dict[Tuple[str, str], int] = defaultdict(int)
    for s in spans:
        if s.kind != "client":
            continue
        callee = s.peer if s.peer in known else server_of_client.get(s.span_id)
        if not callee or callee == s.service:
            continue
        key = (s.service, callee)
        durations[key].append(s.duration_ms)
        errors[key] += 1 if s.is_error else 0
    return {k: Edge(k[0], k[1], len(v), errors[k], percentile(v, 95)) for k, v in durations.items()}


def callees(graph: Dict[Tuple[str, str], Edge], svc: str) -> List[str]:
    return sorted(c for (a, c) in graph if a == svc)


def callers(graph: Dict[Tuple[str, str], Edge], svc: str) -> List[str]:
    return sorted(a for (a, c) in graph if c == svc)


def reachable_downstream(graph: Dict[Tuple[str, str], Edge], svc: str) -> Set[str]:
    seen: Set[str] = set()
    stack = [svc]
    while stack:
        for nxt in callees(graph, stack.pop()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def root_candidates(graph, anomalous: Sequence[str], starved: Set[str]) -> List[str]:
    """Anomalous services with no *non-starved* anomalous service below them.

    A service whose only symptom is receiving less traffic ("starved") is a victim
    of an upstream failure, never a cause, so it neither qualifies nor shadows others.
    """
    live = [s for s in anomalous if s not in starved]
    return [s for s in live if not (reachable_downstream(graph, s) & set(live))]


def propagation_path(graph, root: str, anomalous: Sequence[str]) -> List[str]:
    """Walk from the root up through anomalous callers: root -> ... -> entry point."""
    path, cur, seen = [root], root, {root}
    while True:
        ups = [c for c in callers(graph, cur) if c in anomalous and c not in seen]
        if not ups:
            return path
        cur = ups[0]
        seen.add(cur)
        path.append(cur)
