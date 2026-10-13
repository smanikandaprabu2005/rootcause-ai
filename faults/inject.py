#!/usr/bin/env python3
"""Chaos harness: inject known faults and record ground truth for evaluation.

    python faults/inject.py list
    python faults/inject.py run latency_payments --duration 90
    python faults/inject.py inject --service payments --type latency --params '{"ms": 1500}'
    python faults/inject.py clear [--service payments]

`run` appends a ground-truth record (scenario, true root-cause service, fault
type, start/end timestamps) to eval/ground_truth.jsonl. The Phase 7 evaluation
harness scores the investigator's diagnosis against these records.
Standard library only, so it runs on the host without installing anything.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

SERVICE_URLS = {
    "gateway": "http://localhost:8080",
    "orders": "http://localhost:8081",
    "inventory": "http://localhost:8082",
    "payments": "http://localhost:8083",
    "notifications": "http://localhost:8084",
}
GROUND_TRUTH = Path(__file__).resolve().parent.parent / "eval" / "ground_truth.jsonl"

SCENARIOS = {
    "latency_payments": {
        "service": "payments", "type": "latency", "params": {"ms": 1500, "jitter_ms": 300},
        "description": "Payments responds slowly; orders and gateway latency rise as a symptom.",
    },
    "crash_inventory": {
        "service": "inventory", "type": "crash", "params": {"after_requests": 20},
        "description": "Inventory process exits repeatedly; Docker restarts it.",
    },
    "db_pool_orders": {
        "service": "orders", "type": "db_pool_exhaustion", "params": {"hold_seconds": 300},
        "description": "Orders cannot get a database connection; requests fail with 503.",
    },
    "memory_leak_payments": {
        "service": "payments", "type": "memory_leak", "params": {"mb_per_request": 3},
        "description": "Payments leaks memory until the container is OOM-killed and restarted.",
    },
    "bad_deploy_orders": {
        "service": "orders", "type": "bad_deploy", "params": {"version": "v1.1.0", "error_rate": 0.5},
        "description": "A new orders version starts throwing errors right after a deploy event.",
    },
    "error_burst_inventory": {
        "service": "inventory", "type": "error", "params": {"rate": 0.7, "status": 500},
        "description": "Inventory returns intermittent 500s; failures propagate to orders and gateway.",
    },
    "slow_notifications_trap": {
        "service": "notifications", "type": "latency", "params": {"ms": 4000},
        "description": "Non-critical notifications is slow (orders swallows the timeout). "
                       "Orders latency rises, so the tempting diagnosis, orders, is wrong.",
    },
    "db_pool_payments": {
        "service": "payments", "type": "db_pool_exhaustion", "params": {"hold_seconds": 300},
        "description": "Payments loses its database pool; orders then fails on the payments call.",
    },
}


def _request(method: str, url: str, body=None, timeout: float = 5.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {url} -> HTTP {exc.code}: {exc.read().decode()[:200]}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {url}: {exc.reason} (is `docker compose up` running?)")


def inject(service: str, ftype: str, params: dict) -> None:
    out = _request("POST", f"{SERVICE_URLS[service]}/admin/fault", {"type": ftype, "params": params})
    print(f"injected {ftype} into {service}: {json.dumps(out['active'])}")


def clear(service: str | None = None, wait_s: float = 90.0) -> None:
    """Clear faults, retrying while a crashed service is in restart backoff (the fault's TTL is the backstop)."""
    for name in [service] if service else SERVICE_URLS:
        deadline = time.time() + wait_s
        while True:
            try:
                _request("DELETE", f"{SERVICE_URLS[name]}/admin/fault", timeout=3.0)
                print(f"cleared faults on {name}")
                break
            except SystemExit as exc:  # a crashed service may be mid-restart
                if time.time() >= deadline:
                    print(f"warning: {exc} (the fault will expire on its own)")
                    break
                time.sleep(2)


def run_scenario(name: str, duration: int) -> None:
    sc = SCENARIOS[name]
    run_id = uuid.uuid4().hex[:8]
    start = datetime.now(timezone.utc)
    inject(sc["service"], sc["type"], {**sc["params"], "ttl_s": duration + 15})
    try:
        print(f"[{run_id}] fault active for {duration}s: {sc['description']}")
        time.sleep(duration)
    finally:
        end = datetime.now(timezone.utc)
        clear(sc["service"])
        GROUND_TRUTH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "run_id": run_id, "scenario": name,
            "root_cause_service": sc["service"], "fault_type": sc["type"], "params": sc["params"],
            "start": start.isoformat(), "end": end.isoformat(), "description": sc["description"],
        }
        with GROUND_TRUTH.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"[{run_id}] ground truth appended to {GROUND_TRUTH}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show scenarios")
    p_run = sub.add_parser("run", help="inject a scenario, wait, clear, record ground truth")
    p_run.add_argument("scenario", choices=SCENARIOS)
    p_run.add_argument("--duration", type=int, default=90)
    p_inj = sub.add_parser("inject", help="inject a single fault by hand (no ground-truth record)")
    p_inj.add_argument("--service", required=True, choices=SERVICE_URLS)
    p_inj.add_argument("--type", required=True)
    p_inj.add_argument("--params", default="{}", help="JSON object")
    p_clr = sub.add_parser("clear", help="clear faults")
    p_clr.add_argument("--service", choices=SERVICE_URLS)
    args = parser.parse_args()

    if args.cmd == "list":
        for name, sc in SCENARIOS.items():
            print(f"{name:26} {sc['service']:14} {sc['type']:19} {sc['description']}")
    elif args.cmd == "run":
        run_scenario(args.scenario, args.duration)
    elif args.cmd == "inject":
        inject(args.service, args.type, json.loads(args.params))
    elif args.cmd == "clear":
        clear(args.service)


if __name__ == "__main__":
    main()
