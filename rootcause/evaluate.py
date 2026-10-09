"""Evaluation harness: inject known faults, investigate, score against ground truth.

    python -m rootcause.evaluate --mode synthetic --runs 5     # offline regression benchmark
    python -m rootcause.evaluate --mode live --runs 5          # headline benchmark (Docker stack must be up)

Metrics
  top-1 service accuracy   was the true root-cause service ranked first?
  top-1 strict accuracy    ... and the fault type as well?
  time to diagnosis        wall-clock seconds the investigation took (and detection delay: fault start -> first anomaly)
  false diagnosis rate     of the runs where the system committed to a diagnosis, the fraction that were wrong
  false alarm rate         healthy control runs where the system reported an incident

SYNTHETIC results are a regression/ablation signal on simulated telemetry. Only LIVE results
against the real Docker stack should be quoted as the benchmark.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import baseline as baselines
from . import simulator
from .config import settings
from .ingest import Ingestor
from .investigator import investigate
from .models import NS
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
RPS_CHOICES = [3.0, 5.0, 7.0]
FAULT_S_CHOICES = [60.0, 90.0, 120.0]


def _score(truth: Dict[str, Any], pred: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if pred is None:
        return {"skipped": True}
    control = truth["root_cause_service"] is None
    committed = pred.get("status") == "supported"
    svc_ok = pred.get("service") == truth["root_cause_service"]
    strict_ok = svc_ok and pred.get("fault_type") == truth["fault_type"]
    if control:
        svc_ok = strict_ok = pred.get("status") == "no_incident"
    return {"skipped": False, "service_correct": svc_ok, "strict_correct": strict_ok,
            "committed": committed, "false_alarm": control and pred.get("status") != "no_incident"}


def _run_once(store: Store, truth: Dict[str, Any], with_llm_baseline: bool) -> Dict[str, Any]:
    start, end = int(truth["start"] * NS), int(truth["end"] * NS)
    window, base = (start, end + 10 * NS), (start - int(settings.baseline_seconds * NS), start)
    report = investigate(store, window, base, use_llm=False)
    d = report["diagnosis"]
    root = truth["root_cause_service"]
    onset = next((a["onset_ns"] for a in report["anomalies"] if a["service"] == root), None)
    row: Dict[str, Any] = {
        "scenario": truth["scenario"], "seed": truth.get("seed"), "truth": [root, truth["fault_type"]],
        "rootcause": {"pred": [d["service"], d["fault_type"]], "status": d["status"], "confidence": d["confidence"],
                      "investigation_seconds": report["investigation_seconds"],
                      "detection_delay_s": round((onset - start) / NS, 1) if onset is not None else None,
                      **_score(truth, d)},
    }
    t = time.perf_counter()
    row["error_count"] = {**_score(truth, baselines.error_count(store, window)), "seconds": round(time.perf_counter() - t, 3)}
    if with_llm_baseline:
        t = time.perf_counter()
        pred = baselines.naive_llm(store, window)
        row["naive_llm"] = {**_score(truth, pred), "seconds": round(time.perf_counter() - t, 3),
                            "pred": [pred.get("service"), pred.get("fault_type")] if pred else None}
    return row


def run_synthetic(runs: int, scenarios: List[str], with_llm_baseline: bool) -> List[Dict[str, Any]]:
    rows = []
    for name in scenarios:
        for i in range(runs):
            with tempfile.TemporaryDirectory() as d:
                truth = simulator.generate(name, d, seed=1000 + i, rps=RPS_CHOICES[i % 3],
                                           fault_s=FAULT_S_CHOICES[(i // 3) % 3])
                store = Store()
                Ingestor(store, d).run_once()
                rows.append(_run_once(store, truth, with_llm_baseline))
            print(f"  {name:26} run {i + 1}/{runs}", flush=True)
    return rows


def run_live(runs: int, scenarios: List[str], duration: int, cooldown: int, with_llm_baseline: bool) -> List[Dict[str, Any]]:
    """Drive the real Docker stack through faults/inject.py and score each run."""
    sys.path.insert(0, str(ROOT / "faults"))
    import inject  # noqa: WPS433

    store = Store(settings.database_url)
    ingestor = Ingestor(store, settings.telemetry_dir)
    rows = []
    for name in scenarios:
        sc = inject.SCENARIOS[name]
        for i in range(runs):
            print(f"  {name:26} run {i + 1}/{runs}: injecting for {duration}s", flush=True)
            inject.clear()
            t_start = time.time()
            inject.run_scenario(name, duration)
            t_end = time.time()
            time.sleep(10)  # let the collector and metric reader flush
            ingestor.run_once()
            truth = {"scenario": name, "root_cause_service": sc["service"], "fault_type": sc["type"],
                     "start": t_start, "end": t_end, "seed": i}
            rows.append(_run_once(store, truth, with_llm_baseline))
            print(f"    cooling down {cooldown}s so the next baseline is clean", flush=True)
            time.sleep(cooldown)
    return rows


# --------------------------------------------------------------------------- #
def _rate(vals: List[bool]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    fault_rows = [r for r in rows if r["truth"][0] is not None]
    control_rows = [r for r in rows if r["truth"][0] is None]
    out: Dict[str, Any] = {"runs": len(rows), "fault_runs": len(fault_rows), "control_runs": len(control_rows)}
    for method in ("rootcause", "error_count", "naive_llm"):
        scored = [r[method] for r in fault_rows if method in r and not r[method].get("skipped")]
        if not scored:
            continue
        committed = [s for s in scored if s["committed"]]
        m: Dict[str, Any] = {
            "top1_service_accuracy": _rate([s["service_correct"] for s in scored]),
            "top1_strict_accuracy": _rate([s["strict_correct"] for s in scored]),
            "false_diagnosis_rate": _rate([not s["strict_correct"] for s in committed]),
            "committed_runs": len(committed),
        }
        if method == "rootcause":
            secs = [s["investigation_seconds"] for s in scored]
            delays = [s["detection_delay_s"] for s in scored if s["detection_delay_s"] is not None]
            m["investigation_seconds_median"] = round(statistics.median(secs), 2)
            m["investigation_seconds_p95"] = round(sorted(secs)[int(0.95 * (len(secs) - 1))], 2)
            m["detection_delay_s_median"] = round(statistics.median(delays), 1) if delays else None
        else:
            m["seconds_median"] = round(statistics.median(r[method]["seconds"] for r in fault_rows if method in r), 2)
        ctrl = [r[method] for r in control_rows if method in r and not r[method].get("skipped")]
        m["false_alarm_rate"] = _rate([c["false_alarm"] for c in ctrl])
        out[method] = m
    by_scenario: Dict[str, Dict[str, Any]] = {}
    for r in fault_rows:
        s = by_scenario.setdefault(r["scenario"], {"n": 0, "rootcause": 0, "error_count": 0})
        s["n"] += 1
        s["rootcause"] += r["rootcause"]["strict_correct"]
        s["error_count"] += r["error_count"]["strict_correct"]
    out["by_scenario"] = by_scenario
    return out


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def render_markdown(summary: Dict[str, Any], mode: str) -> str:
    banner = ("> **Synthetic mode**: simulated telemetry, a regression/ablation signal. "
              "Not the headline benchmark.\n\n" if mode == "synthetic" else
              "> **Live mode**: real Docker stack, real injected faults.\n\n")
    lines = [f"# Evaluation results ({mode})\n", banner,
             f"{summary['fault_runs']} fault runs, {summary['control_runs']} healthy control runs.\n",
             "| Method | Top-1 service | Top-1 service+fault | False-diagnosis rate | False-alarm rate (control) | Time per diagnosis |",
             "|---|---|---|---|---|---|"]
    names = {"rootcause": "**RootCause AI**", "error_count": "Baseline: blame most-errors service",
             "naive_llm": "Baseline: dump logs into an LLM"}
    for k in ("rootcause", "error_count", "naive_llm"):
        if k not in summary:
            continue
        m = summary[k]
        t = (f"{m['investigation_seconds_median']} s median" if k == "rootcause" else f"{m['seconds_median']} s median")
        lines.append(f"| {names[k]} | {_pct(m['top1_service_accuracy'])} | {_pct(m['top1_strict_accuracy'])} | "
                     f"{_pct(m['false_diagnosis_rate'])} | {_pct(m['false_alarm_rate'])} | {t} |")
    lines.append("\nTime per diagnosis is compute time once an incident window is known (as an alert would supply it). "
                 "Detection latency is not reported: every window here starts at the fault, so it would read ~0 s "
                 "by construction.")
    lines += ["\n## Per scenario (strict accuracy: service and fault type)\n",
              "| Scenario | Runs | RootCause AI | Most-errors baseline |", "|---|---|---|---|"]
    for name, s in summary["by_scenario"].items():
        lines.append(f"| {name} | {s['n']} | {s['rootcause']}/{s['n']} | {s['error_count']}/{s['n']} |")
    if "naive_llm" not in summary:
        lines.append("\n_The naive-LLM baseline was skipped (set GEMINI_API_KEY to include it)._")
    return "\n".join(lines) + "\n"


def main() -> None:
    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["synthetic", "live"], default="synthetic")
    ap.add_argument("--runs", type=int, default=5, help="runs per scenario")
    ap.add_argument("--scenarios", nargs="*", default=None)
    ap.add_argument("--duration", type=int, default=90, help="live: seconds each fault stays active")
    ap.add_argument("--cooldown", type=int, default=150, help="live: healthy seconds between runs")
    ap.add_argument("--out", default=str(ROOT / "eval"))
    args = ap.parse_args()

    from . import llm

    names = args.scenarios or (list(simulator.SCENARIOS) if args.mode == "synthetic"
                               else [n for n in simulator.SCENARIOS if n != "no_fault_control"])
    print(f"{args.mode} evaluation: {len(names)} scenarios x {args.runs} runs", flush=True)
    rows = (run_synthetic(args.runs, names, llm.available()) if args.mode == "synthetic"
            else run_live(args.runs, names, args.duration, args.cooldown, llm.available()))
    summary = summarize(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"results_{args.mode}.json").write_text(json.dumps({"summary": summary, "runs": rows}, indent=1))
    md = render_markdown(summary, args.mode)
    (out / f"RESULTS_{args.mode}.md").write_text(md)
    print("\n" + md)


if __name__ == "__main__":
    main()
