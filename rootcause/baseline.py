"""Baselines the investigator is compared against.

* ``error_count``: "grep the errors": blame the service that logged the most ERROR lines.
* ``naive_llm``: dump the window's warning/error logs into an LLM and ask for the root cause
  (no traces, no metrics, no dependency graph, no hypothesis testing). Needs GEMINI_API_KEY.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, Optional

from . import llm
from .hypotheses import FAULT_TYPES
from .models import NS
from .store import Store

_MAX_LOG_LINES = 300


def error_count(store: Store, window: tuple) -> Dict[str, Any]:
    logs = [r for r in store.logs(*window) if r.severity in ("ERROR", "CRITICAL")]
    if not logs:
        return {"service": None, "fault_type": None, "status": "no_incident"}
    svc = Counter(r.service for r in logs).most_common(1)[0][0]
    return {"service": svc, "fault_type": "error", "status": "supported"}


def naive_llm(store: Store, window: tuple) -> Optional[Dict[str, Any]]:
    """Returns None when no LLM is configured, so the comparison is skipped rather than faked."""
    if not llm.available():
        return None
    logs = [r for r in store.logs(*window) if r.severity in ("WARNING", "ERROR", "CRITICAL")][:_MAX_LOG_LINES]
    lines = "\n".join(f"{(r.ts_ns - window[0]) / NS:7.1f}s {r.service:13} {r.severity:8} {r.body}" for r in logs)
    prompt = (
        "A microservice shop (gateway, orders, inventory, payments, notifications) is having an incident. "
        "Here are its warning/error logs. Identify the single root-cause service and fault type.\n"
        f"Fault types: {', '.join(FAULT_TYPES)}.\n"
        'Reply with JSON only: {"service": "...", "fault_type": "..."}\n\n' + lines
    )
    text = llm.generate(prompt)
    if not text:
        return {"service": None, "fault_type": None, "status": "failed"}
    match = re.search(r"\{.*?\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    return {"service": data.get("service"), "fault_type": data.get("fault_type"), "status": "supported"}
