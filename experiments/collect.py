"""Collect time-series metrics during an autoscaling experiment run.

All metrics come from Prometheus (the required monitoring tool), via per-pod
service discovery, so the comparison is sourced exactly as the assignment asks.

Records one CSV row every --interval seconds:
    timestamp, p99_latency, replica_count, cpu_cores, queue_depth

- p99_latency  : SERVER-SIDE service p99 (dispatcher_request_duration_seconds) —
                 the graded SLO metric (< 0.5 s): queue wait + inference, i.e. the
                 latency a query experiences in the service.
- replica_count: number of inference pods currently scraped (sum(up{job=inference})).
- cpu_cores    : CPU cores used by the inference pods
                 (sum(rate(process_cpu_seconds_total{job=inference}[1m]))).
- queue_depth  : dispatcher_queue_depth — the metric the custom autoscaler reacts
                 to (kept as a diagnostic; not part of the graded figure).

Run one instance per experiment (Prometheus must be reachable, e.g. via
`kubectl -n inference-system port-forward svc/prometheus 9090:9090`):
    python experiments/collect.py --out custom.csv
    python experiments/collect.py --out hpa70.csv
    python experiments/collect.py --out hpa90.csv

Stop it with Ctrl-C (or --duration) when the load-tester Job finishes.
"""
import argparse
import csv
import math
import sys
import time
from pathlib import Path

import requests

# Server-side service p99 (the graded SLO metric): queue wait + inference.
P99_QUERY = (
    'histogram_quantile(0.99, '
    'sum(rate(dispatcher_request_duration_seconds_bucket[1m])) by (le))'
)
# Replicas and CPU cores, both from Prometheus per-pod scraping.
REPLICAS_QUERY = 'sum(up{job="inference"})'
CPU_QUERY = 'sum(rate(process_cpu_seconds_total{job="inference"}[1m]))'
# Diagnostic: what the custom autoscaler reacts to.
QUEUE_DEPTH_QUERY = "dispatcher_queue_depth"


def query_prometheus_scalar(prom_url: str, query: str) -> float:
    """Return the first scalar value of an instant PromQL query, or NaN."""
    try:
        resp = requests.get(
            f"{prom_url.rstrip('/')}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()["data"]["result"]
        if not results:
            return math.nan
        return float(results[0]["value"][1])
    except Exception as exc:  # noqa: BLE001 - best-effort sampling
        print(f"[warn] prometheus query failed: {exc}", file=sys.stderr)
        return math.nan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect experiment metrics to CSV")
    parser.add_argument("--out", required=True, help="Output CSV path")
    parser.add_argument(
        "--prometheus-url",
        default="http://localhost:9090",
        help="Prometheus base URL (use a port-forward when running locally)",
    )
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Total seconds to sample (0 = until Ctrl-C)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()

    print(f"Collecting -> {out_path} every {args.interval}s (Ctrl-C to stop)")
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["timestamp", "p99_latency", "replica_count", "cpu_cores", "queue_depth"]
        )
        try:
            while True:
                now = time.time()
                p99 = query_prometheus_scalar(args.prometheus_url, P99_QUERY)
                replicas = query_prometheus_scalar(args.prometheus_url, REPLICAS_QUERY)
                cpu = query_prometheus_scalar(args.prometheus_url, CPU_QUERY)
                queue = query_prometheus_scalar(args.prometheus_url, QUEUE_DEPTH_QUERY)
                writer.writerow([round(now, 3), p99, replicas, cpu, queue])
                handle.flush()
                print(
                    f"t={int(now - start):4d}s p99={p99:.3f} "
                    f"replicas={replicas:.0f} cpu={cpu:.3f} queue={queue:.1f}"
                )
                if args.duration and (now - start) >= args.duration:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
    print(f"done -> {out_path}")


if __name__ == "__main__":
    main()
