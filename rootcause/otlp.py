"""Parse the OpenTelemetry Collector's JSON-lines file-exporter output.

Each line is one OTLP/JSON export request (``resourceSpans`` / ``resourceMetrics``
/ ``resourceLogs``). The parser is deliberately tolerant: ids may be hex or
base64-free strings, enums may be ints or names, and both the old and new HTTP
semantic-convention attribute names are understood.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from .models import LogRec, MetricPoint, Span

_KIND = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
_KIND_NAME = {"SPAN_KIND_INTERNAL": "internal", "SPAN_KIND_SERVER": "server", "SPAN_KIND_CLIENT": "client"}


def _val(v: Dict[str, Any]) -> Any:
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    return None


def attrs_to_dict(attrs: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    return {a["key"]: _val(a.get("value", {})) for a in (attrs or [])}


def _service(resource: Dict[str, Any]) -> str:
    return str(attrs_to_dict(resource.get("attributes")).get("service.name", "unknown"))


def _kind(raw: Any) -> str:
    if isinstance(raw, int):
        return _KIND.get(raw, "internal")
    return _KIND_NAME.get(str(raw), "internal")


def _status(raw: Optional[Dict[str, Any]]) -> str:
    code = (raw or {}).get("code", 0)
    if code in (2, "STATUS_CODE_ERROR"):
        return "error"
    if code in (1, "STATUS_CODE_OK"):
        return "ok"
    return "unset"


def _peer(attrs: Dict[str, Any]) -> Optional[str]:
    url = attrs.get("http.url") or attrs.get("url.full")
    if url:
        return urlparse(str(url)).hostname
    return attrs.get("server.address") or attrs.get("net.peer.name")


def parse_spans(request: Dict[str, Any]) -> List[Span]:
    out: List[Span] = []
    for rs in request.get("resourceSpans", []):
        service = _service(rs.get("resource", {}))
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                a = attrs_to_dict(s.get("attributes"))
                http_status = a.get("http.status_code", a.get("http.response.status_code"))
                kind = _kind(s.get("kind", 1))
                out.append(
                    Span(
                        trace_id=s["traceId"], span_id=s["spanId"], parent_id=s.get("parentSpanId") or None,
                        service=service, name=s.get("name", ""), kind=kind,
                        start_ns=int(s["startTimeUnixNano"]), end_ns=int(s["endTimeUnixNano"]),
                        status=_status(s.get("status")),
                        http_status=int(http_status) if http_status is not None else None,
                        peer=_peer(a) if kind == "client" else None,
                        attrs={k: v for k, v in a.items() if k in ("http.method", "http.target", "http.route", "http.url")},
                    )
                )
    return out


def parse_logs(request: Dict[str, Any]) -> List[LogRec]:
    out: List[LogRec] = []
    for rl in request.get("resourceLogs", []):
        service = _service(rl.get("resource", {}))
        for sl in rl.get("scopeLogs", []):
            for r in sl.get("logRecords", []):
                a = attrs_to_dict(r.get("attributes"))
                body = _val(r.get("body", {})) or ""
                out.append(
                    LogRec(
                        ts_ns=int(r.get("timeUnixNano") or r.get("observedTimeUnixNano") or 0),
                        service=service, severity=str(r.get("severityText", "INFO")).upper(),
                        body=str(body), event=a.get("event"),
                        trace_id=r.get("traceId") or None, span_id=r.get("spanId") or None, attrs=a,
                    )
                )
    return out


def parse_metrics(request: Dict[str, Any]) -> List[MetricPoint]:
    out: List[MetricPoint] = []
    for rm in request.get("resourceMetrics", []):
        service = _service(rm.get("resource", {}))
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                body = m.get("gauge") or m.get("sum")
                if not body:
                    continue  # histograms are not needed: latency comes from traces
                for dp in body.get("dataPoints", []):
                    value = dp.get("asDouble", dp.get("asInt"))
                    if value is None:
                        continue
                    out.append(
                        MetricPoint(
                            ts_ns=int(dp["timeUnixNano"]), service=service, name=m["name"],
                            value=float(value), attrs=attrs_to_dict(dp.get("attributes")),
                        )
                    )
    return out


def parse_line(kind: str, line: str) -> Tuple[List[Span], List[LogRec], List[MetricPoint]]:
    request = json.loads(line)
    if kind == "traces":
        return parse_spans(request), [], []
    if kind == "logs":
        return [], parse_logs(request), []
    return [], [], parse_metrics(request)


def iter_lines(path) -> Iterable[str]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield line
