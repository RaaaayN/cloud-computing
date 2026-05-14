#!/usr/bin/env python3
"""Hammer POST /predict from localhost; prints rough QPS once per second."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_URL = "http://127.0.0.1:55616"
PREDICT_PATH = "/predict"
TARGET_URL = BASE_URL.rstrip("/") + PREDICT_PATH

MAX_WORKERS = 20

_counter = 0
_counter_lock = threading.Lock()


def _post_once() -> None:
    # pass cpu_spin_ms so backend can burn CPU (helps CPU HPA demos)
    data = json.dumps({"load_test": True, "cpu_spin_ms": 200}).encode("utf-8")
    req = urllib.request.Request(
        TARGET_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()


def worker() -> None:
    global _counter
    while True:
        try:
            _post_once()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
            pass  # still count attempt
        finally:
            with _counter_lock:
                _counter += 1


def qps_reporter() -> None:
    prev = 0
    while True:
        time.sleep(1.0)
        with _counter_lock:
            now = _counter
        qps = now - prev
        prev = now
        print(f"QPS: {qps}  (total {now})", flush=True)


def main() -> None:
    print(f"POST {TARGET_URL}", flush=True)
    print(f"threads={MAX_WORKERS}, ctrl+c to stop", flush=True)

    threading.Thread(target=qps_reporter, daemon=True).start()

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        for _ in range(MAX_WORKERS):
            executor.submit(worker)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        executor.shutdown(wait=False, cancel_futures=False)


if __name__ == "__main__":
    main()
