"""The investigation pipeline, orchestrated as a LangGraph state machine.

    correlate (Agent 1) -> dependency (Agent 2) -> hypothesize (Agent 3) -> timeline -> conclude
                 \\-> (no anomalies) -> conclude

Each node reads and extends a shared state, so the whole run is inspectable and
individual agents can be tested or swapped independently.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import END, StateGraph

from . import anomaly as anomaly_mod
from . import graph as graph_mod
from . import hypotheses as hyp_mod
from . import llm
from .config import settings
from .models import NS
from .store import Store
from .timeline import build_timeline

FAULT_DESC = {
    "latency": "a latency fault: the service itself is slow",
    "crash": "an application crash loop",
    "db_pool_exhaustion": "database connection-pool exhaustion",
    "memory_leak": "a memory leak ending in OOM kills",
    "bad_deploy": "a bad deployment",
    "error": "a source of intermittent errors",
}


class State(TypedDict, total=False):
    store: Store
    window: Tuple[int, int]
    baseline: Tuple[int, int]
    use_llm: bool
    ctx: Any
    anomalies: list
    starved: list
    graph: dict
    roots: list
    path: list
    hypotheses: list
    timeline: list
    report: dict


def correlate(state: State) -> State:
    """Agent 1: correlate logs, traces and metrics; find which services deviate from their baseline."""
    ctx = hyp_mod.Context(state["store"], state["window"], state["baseline"])
    found = anomaly_mod.detect(
        ctx.all_spans, ctx.b_mets + ctx.w_mets, state["baseline"], state["window"], ctx.services,
        int(settings.bucket_seconds * NS),
    )
    starved = [a.service for a in found if set(a.deviating) == {"requests"}]
    return {"ctx": ctx, "anomalies": found, "starved": starved}


def dependency(state: State) -> State:
    """Agent 2: build the service dependency graph and reason about failure propagation."""
    ctx = state["ctx"]
    g = graph_mod.build_graph(ctx.w_spans or ctx.all_spans, ctx.services)
    names = [a.service for a in state["anomalies"]]
    roots = graph_mod.root_candidates(g, names, set(state["starved"]))
    return {"graph": g, "roots": roots}


def hypothesize(state: State) -> State:
    """Agent 3: generate competing root-cause hypotheses and try to falsify each against the evidence."""
    names = [a.service for a in state["anomalies"]]
    hyps = hyp_mod.evaluate(state["ctx"], names, state["graph"], state["roots"], state["starved"])
    path: List[str] = []
    live = [h for h in hyps if h.status != "rejected"]
    if live:
        path = graph_mod.propagation_path(state["graph"], live[0].service, names)
    return {"hypotheses": hyps, "path": path}


def timeline(state: State) -> State:
    return {"timeline": build_timeline(state["ctx"], state["anomalies"], set(state["path"]))}


def conclude(state: State) -> State:
    ctx = state.get("ctx")
    window = state["window"]
    graph = state.get("graph", {})
    report: Dict[str, Any] = {
        "window": {"start_ns": window[0], "end_ns": window[1], "baseline_start_ns": state["baseline"][0]},
        "anomalies": [a.to_dict() for a in state.get("anomalies", [])],
        "graph": {
            "nodes": [{"id": s, "anomalous": s in {a.service for a in state.get("anomalies", [])},
                       "starved": s in state.get("starved", [])} for s in (ctx.services if ctx else [])],
            "edges": [e.to_dict() for e in graph.values()],
        },
        "root_candidates": state.get("roots", []),
        "propagation_path": state.get("path", []),
        "hypotheses": [h.to_dict() for h in state.get("hypotheses", [])],
        "timeline": state.get("timeline", []),
    }
    hyps = state.get("hypotheses", [])
    if not state.get("anomalies"):
        report["diagnosis"] = {"status": "no_incident", "service": None, "fault_type": None, "confidence": 0.0,
                               "summary": "No service deviates from its baseline in this window."}
    else:
        live = [h for h in hyps if h.status != "rejected"]
        best = live[0] if live else None
        ruled_out = [f"{h.service}/{h.fault_type}: {h.rejected_because}" for h in hyps
                     if h.status == "rejected" and h.service in (state["path"] or [])][:6]
        if best is None:
            report["diagnosis"] = {"status": "inconclusive", "service": None, "fault_type": None, "confidence": 0.0,
                                   "summary": "Anomalies were detected but every hypothesis was falsified.",
                                   "ruled_out": ruled_out}
        else:
            status = "supported" if best.status == "supported" else "inconclusive"
            supporting = [e.summary for e in best.evidence if e.supports and e.kind != "graph"]
            summary = (f"{best.service}: {FAULT_DESC[best.fault_type]}. " + "; ".join(supporting[:3]) + ".")
            report["diagnosis"] = {
                "status": status, "service": best.service, "fault_type": best.fault_type,
                "confidence": round(best.confidence, 3), "score": round(best.score, 2), "summary": summary,
                "propagation": state["path"], "ruled_out": ruled_out,
                "runner_up": next(({"service": h.service, "fault_type": h.fault_type, "score": round(h.score, 2)}
                                   for h in live[1:2]), None),
            }
    narrative = llm.narrate(report) if state.get("use_llm", True) else None
    report["narrative"] = narrative or report["diagnosis"]["summary"]
    report["narrative_source"] = "llm" if narrative else "evidence"
    return {"report": report}


def _route_after_correlate(state: State) -> str:
    return "dependency" if state.get("anomalies") else "conclude"


def build_graph() -> Any:
    g = StateGraph(State)
    for name, fn in [("correlate", correlate), ("dependency", dependency), ("hypothesize", hypothesize),
                     ("timeline", timeline), ("conclude", conclude)]:
        g.add_node(name, fn)
    g.set_entry_point("correlate")
    g.add_conditional_edges("correlate", _route_after_correlate, {"dependency": "dependency", "conclude": "conclude"})
    g.add_edge("dependency", "hypothesize")
    g.add_edge("hypothesize", "timeline")
    g.add_edge("timeline", "conclude")
    g.add_edge("conclude", END)
    return g.compile()


_PIPELINE = None


def investigate(store: Store, window: Optional[Tuple[int, int]] = None, baseline: Optional[Tuple[int, int]] = None,
                use_llm: bool = True, save: bool = False, as_of_ns: Optional[int] = None) -> Dict[str, Any]:
    """Run the full investigation and return the report (a JSON-serialisable dict).

    With no ``window`` the incident is *discovered* in the telemetry (see incident.py), as of ``as_of_ns`` (default:
    the newest telemetry). Pass a window explicitly when an alert already says when the problem started."""
    global _PIPELINE
    if _PIPELINE is None:
        _PIPELINE = build_graph()
    bounds = store.time_bounds()
    if bounds is None:
        raise ValueError("no telemetry has been ingested yet")
    started = time.perf_counter()
    discovery = None
    if window is None:
        from .incident import find_incident

        found = find_incident(store, as_of_ns)
        window, discovery = found.window, found
        if baseline is None:
            baseline = found.baseline
    if baseline is None:
        baseline = (max(bounds[0], window[0] - int(settings.baseline_seconds * NS)), window[0])
    out = _PIPELINE.invoke({"store": store, "window": window, "baseline": baseline, "use_llm": use_llm})
    report = out["report"]
    report["id"] = uuid.uuid4().hex[:12]
    report["investigation_seconds"] = round(time.perf_counter() - started, 3)
    report["window_source"] = "discovered" if discovery else "given"
    if discovery:
        report["discovery"] = discovery.to_dict()
    if save:
        store.save_investigation(report["id"], report)
    return report
