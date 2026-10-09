"""Steady background traffic so there is always telemetry to investigate."""
import os
import random
import time

import requests

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080")
RATE = float(os.getenv("RATE", "5"))  # requests per second


def main() -> None:
    session = requests.Session()
    ok = failed = 0
    last_report = time.time()
    while True:
        started = time.time()
        try:
            if random.random() < 0.15:
                r = session.get(f"{GATEWAY}/products", timeout=5)
            else:
                r = session.post(
                    f"{GATEWAY}/orders",
                    json={"item_id": random.randint(1, 20), "qty": random.randint(1, 3)},
                    timeout=5,
                )
            ok, failed = (ok + 1, failed) if r.status_code < 400 else (ok, failed + 1)
        except requests.RequestException:
            failed += 1
        if time.time() - last_report >= 10:
            print(f"loadgen ok={ok} failed={failed}", flush=True)
            last_report = time.time()
        time.sleep(max(0.0, 1.0 / RATE - (time.time() - started)))


if __name__ == "__main__":
    main()
