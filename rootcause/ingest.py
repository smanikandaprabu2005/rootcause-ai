"""Incrementally load the collector's JSON-lines files into the store."""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Dict

from .otlp import parse_line
from .store import Store, open_store

log = logging.getLogger("rootcause.ingest")
SOURCES = ("traces", "metrics", "logs")


class Ingestor:
    def __init__(self, store: Store, telemetry_dir: str | Path):
        self.store = store
        self.dir = Path(telemetry_dir)

    def run_once(self) -> Dict[str, int]:
        """Read any new complete lines from each file. Safe to call repeatedly."""
        added = {k: 0 for k in SOURCES}
        for kind in SOURCES:
            path = self.dir / f"{kind}.jsonl"
            if not path.exists():
                continue
            key = str(path)
            offset = self.store.get_offset(key)
            size = path.stat().st_size
            if offset > size:  # file rotated or truncated: start over
                offset = 0
            spans, logs, points = [], [], []
            with open(path, "rb") as fh:
                fh.seek(offset)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):  # partial write: pick it up next time
                        break
                    offset += len(line)
                    if not line.strip():
                        continue
                    try:
                        s, l, m = parse_line(kind, line.decode("utf-8"))
                    except (ValueError, KeyError) as exc:
                        log.warning("skipping malformed %s line at byte %d: %s", kind, offset, exc)
                        continue
                    spans += s
                    logs += l
                    points += m
            if spans:
                self.store.add_spans(spans)
            if logs:
                self.store.add_logs(logs)
            if points:
                self.store.add_metrics(points)
            self.store.set_offset(key, offset)
            added[kind] = len(spans) + len(logs) + len(points)
        return added

    def watch(self, interval_s: float = 5.0) -> None:
        log.info("watching %s", self.dir)
        while True:
            added = self.run_once()
            if any(added.values()):
                log.info("ingested %s", added)
            time.sleep(interval_s)


def main() -> None:
    from .config import settings

    parser = argparse.ArgumentParser(description="Ingest collector JSONL telemetry into the store")
    parser.add_argument("--dir", default=settings.telemetry_dir)
    parser.add_argument("--db", default=settings.database_url)
    parser.add_argument("--watch", action="store_true", help="keep running and ingest new data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ing = Ingestor(open_store(args.db), args.dir)
    if args.watch:
        ing.watch()
    else:
        print(ing.run_once())


if __name__ == "__main__":
    main()
