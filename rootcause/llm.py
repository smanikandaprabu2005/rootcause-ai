"""Optional Gemini integration. The investigator works without it; the LLM only
*narrates* a diagnosis that the evidence engine already produced, and is told to
use nothing but the evidence it is handed."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional

from .config import settings

_NARRATE = """You are an SRE writing the summary of an incident investigation.
Use ONLY the facts in the JSON below. Do not add causes, numbers or services that
are not present. Write 3-5 sentences: the root cause, the key evidence, how the
failure propagated, and what was ruled out. Cite evidence by its `ref` in square brackets.

{payload}"""


def available() -> bool:
    return bool(settings.gemini_api_key)


def generate(prompt: str) -> Optional[str]:
    if not available():
        return None
    try:
        from google import genai

        client = genai.Client(api_key=settings.gemini_api_key)
        resp = client.models.generate_content(model=settings.gemini_model, contents=prompt)
        return (resp.text or "").strip() or None
    except Exception:  # network / quota / SDK errors must never break an investigation
        return None


_CITATION = re.compile(r"\[([^\[\]]+)\]")


def citations_are_valid(text: str, allowed_refs: Iterable[str]) -> bool:
    """A narrative is only accepted if it cites evidence and every citation is a real ref we handed it.

    This is the guardrail that keeps the LLM a narrator: a summary that invents a source, or cites
    nothing at all, is discarded and the evidence-built summary is used instead."""
    cited = [c.strip() for m in _CITATION.findall(text) for c in m.split(",")]
    return bool(cited) and all(c in set(allowed_refs) for c in cited)


def narrate(report: Dict[str, Any]) -> Optional[str]:
    d = report["diagnosis"]
    if d.get("status") == "no_incident":
        return None
    live = [h for h in report["hypotheses"] if h.get("status") != "rejected"]
    best = live[0] if live else {}
    supporting = [e for e in best.get("evidence", []) if e["supports"] and e.get("ref")]
    payload = {
        "diagnosis": {k: d.get(k) for k in ("service", "fault_type", "confidence", "propagation")},
        "supporting_evidence": supporting,
        "ruled_out": d.get("ruled_out", []),
    }
    text = generate(_NARRATE.format(payload=json.dumps(payload, indent=1)))
    if text and citations_are_valid(text, {e["ref"] for e in supporting}):
        return text
    return None
