"""Command line: ``python -m rootcause <command>``.

    ingest        read the collector's JSONL files into the database (once, or --watch to follow)
    investigate   investigate the most recent window (or --last SECONDS) and print the report
    serve         run the API (and the dashboard, if it has been built)
    simulate      write simulated telemetry for a scenario into a directory
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings

from .config import settings
from .ingest import Ingestor
from .models import NS
from .store import Store


def main(argv=None) -> int:
    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser(prog="rootcause", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest")
    p.add_argument("--watch", action="store_true")
    p = sub.add_parser("investigate")
    p.add_argument("--last", type=float, default=90.0, help="investigate the last N seconds of telemetry")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--remediate", action="store_true", help="also verify a fix in a disposable sandbox")
    p.add_argument("--json", action="store_true", help="print the full JSON report")
    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("simulate")
    p.add_argument("scenario")
    p.add_argument("out_dir")
    p.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    if args.cmd == "simulate":
        from . import simulator

        print(json.dumps(simulator.generate(args.scenario, args.out_dir, seed=args.seed), indent=1))
        return 0
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("rootcause.api:get_app", factory=True, host=args.host, port=args.port)
        return 0

    store = Store(settings.database_url)
    ingestor = Ingestor(store, settings.telemetry_dir)
    if args.cmd == "ingest":
        if args.watch:
            ingestor.watch()
        else:
            print(json.dumps(ingestor.run_once()))
        return 0

    from .investigator import investigate
    from .remediation import plan_and_verify

    ingestor.run_once()
    bounds = store.time_bounds()
    if bounds is None:
        print("no telemetry found; is the stack running and TELEMETRY_DIR correct?", file=sys.stderr)
        return 1
    window = (max(bounds[0], bounds[1] - int(args.last * NS)), bounds[1])
    report = investigate(store, window, use_llm=not args.no_llm, save=True)
    if args.remediate:
        report["remediation"] = plan_and_verify(report)
        store.save_investigation(report["id"], report)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        d = report["diagnosis"]
        print(f"[{d['status']}] {d.get('service')} / {d.get('fault_type')}  confidence {d['confidence']:.0%}")
        print(report["narrative"])
        for e in report["timeline"][:12]:
            print(f"  +{e['t_rel_s']:>6}s  {e['service']:<13} {e['summary']}")
        if "remediation" in report:
            r = report["remediation"]
            print(f"remediation: {r['status']}" + (f" ({r['reason']})" if r.get("reason") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
