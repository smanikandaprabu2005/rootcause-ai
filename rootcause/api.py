"""FastAPI service: telemetry queries, investigations and evidence lookup."""
from __future__ import annotations

import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .features import percentile
from .graph import build_graph
from .ingest import Ingestor
from .investigator import investigate
from .models import NS
from .store import Store, open_store


class InvestigateRequest(BaseModel):
    window_start_ns: Optional[int] = None
    window_end_ns: Optional[int] = None
    baseline_seconds: Optional[float] = None
    use_llm: bool = True
    remediate: bool = False


def create_app(store: Optional[Store] = None, telemetry_dir: Optional[str] = None,
               sandbox: Any = None) -> FastAPI:
    store = store or open_store(settings.database_url)
    ingestor = Ingestor(store, telemetry_dir or settings.telemetry_dir)
    app = FastAPI(title="RootCause AI", version="1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    if os.getenv("ROOTCAUSE_WATCH") == "1":
        threading.Thread(target=ingestor.watch, daemon=True, name="ingest-watch").start()

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def status() -> Dict[str, Any]:
        bounds = store.time_bounds()
        return {"counts": store.counts(), "services": store.services(),
                "start_ns": bounds[0] if bounds else None, "end_ns": bounds[1] if bounds else None,
                "llm_enabled": bool(settings.gemini_api_key)}

    @app.post("/api/ingest")
    def ingest() -> Dict[str, int]:
        return ingestor.run_once()

    @app.get("/api/graph")
    def graph(window_seconds: float = 60.0) -> Dict[str, Any]:
        bounds = store.time_bounds()
        if not bounds:
            return {"nodes": [], "edges": []}
        spans = store.spans(max(bounds[0], bounds[1] - int(window_seconds * NS)), bounds[1])
        services = store.services()
        edges = build_graph(spans, services)
        return {"nodes": [{"id": s} for s in services], "edges": [e.to_dict() for e in edges.values()]}

    @app.get("/api/services")
    def services(window_seconds: float = 60.0) -> Any:
        bounds = store.time_bounds()
        if not bounds:
            return []
        spans = store.spans(max(bounds[0], bounds[1] - int(window_seconds * NS)), bounds[1])
        out = []
        for svc in store.services():
            srv = [s for s in spans if s.service == svc and s.kind == "server"]
            out.append({
                "service": svc, "requests": len(srv),
                "error_rate": round(sum(1 for s in srv if s.is_error) / len(srv), 4) if srv else 0.0,
                "p95_ms": round(percentile([s.duration_ms for s in srv], 95), 1),
            })
        return out

    @app.post("/api/investigate")
    def run_investigation(req: InvestigateRequest) -> Dict[str, Any]:
        window = baseline = None
        if req.window_start_ns is not None and req.window_end_ns is not None:
            window = (req.window_start_ns, req.window_end_ns)
            base_s = req.baseline_seconds if req.baseline_seconds is not None else settings.baseline_seconds
            baseline = (window[0] - int(base_s * NS), window[0])
        try:
            report = investigate(store, window, baseline, use_llm=req.use_llm, save=False)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if req.remediate:
            from .remediation import plan_and_verify

            report["remediation"] = plan_and_verify(report, sandbox=sandbox)
        store.save_investigation(report["id"], report)
        return report

    @app.get("/api/investigations")
    def list_investigations() -> Any:
        return store.list_investigations()

    @app.get("/api/investigations/{inv_id}")
    def get_investigation(inv_id: str) -> Dict[str, Any]:
        report = store.get_investigation(inv_id)
        if report is None:
            raise HTTPException(status_code=404, detail="unknown investigation")
        return report

    @app.get("/api/evidence")
    def evidence(ref: str) -> Dict[str, Any]:
        """Resolve an evidence reference to the raw telemetry behind it."""
        if ref.startswith("log:") and ref[4:].isdigit():
            rec = store.get_log(int(ref[4:]))
            if rec is None:
                raise HTTPException(status_code=404, detail="log not found")
            return {"ref": ref, "type": "log", "record": {k: v for k, v in asdict(rec).items()}}
        if ref.startswith("trace:"):
            trace_id = ref.split("/")[0][len("trace:"):]
            spans = store.get_trace(trace_id)
            if not spans:
                raise HTTPException(status_code=404, detail="trace not found")
            return {"ref": ref, "type": "trace", "spans": [
                {**asdict(s), "duration_ms": round(s.duration_ms, 2)} for s in spans]}
        return {"ref": ref, "type": "derived", "note": "Aggregate statistic or metric series computed from raw telemetry."}

    dist = Path(os.getenv("DASHBOARD_DIST", Path(__file__).resolve().parent.parent / "dashboard" / "dist"))
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    return app


def get_app() -> FastAPI:  # uvicorn factory: uvicorn rootcause.api:get_app --factory
    return create_app()
