"""Collect time-series metrics during an autoscaling experiment run.

Records one CSV row every --interval seconds:
    timestamp, p99_latency, e2e_p99, queue_depth, arrival_rate, replica_count, cpu_cores

- p99_latency  : server-side inference p99 (inference_duration_seconds).
- e2e_p99      : end-to-end client p99 (loadtester_request_duration_seconds) — the
                 real SLO metric; unlike server p99 it includes time spent waiting
                 in the dispatcher queue, so it exposes the cost of under-scaling.
- queue_depth  : dispatcher_queue_depth — what the custom autoscaler reacts to.
- arrival_rate : rate(dispatcher_requests_total[1m]) — load level (same trace for all).
- replica_count: ready replicas of the inference Deployment, read from the
                 Kubernetes API (the Prometheus config scrapes the Service DNS,
                 so `up{job=...}` cannot count replicas).
- cpu_cores    : total CPU cores used by inference pods, read from metrics.k8s.io
                 (metrics-server, which is required for the HPA anyway).

Run one instance per experiment, e.g.:
    python experiments/collect.py --out custom.csv
    python experiments/collect.py --out hpa70.csv
    python experiments/collect.py --out hpa90.csv

Stop it with Ctrl-C (or --duration) when the load-tester job finishes.
"""
import argparse
import csv
import math
import sys
import time
from pathlib import Path

import requests

P99_QUERY = (
    "histogram_quantile(0.99, "
    "sum(rate(inference_duration_seconds_bucket[1m])) by (le))"
)
# End-to-end client p99 (includes dispatcher queue wait) — the real SLO metric.
E2E_P99_QUERY = (
    "histogram_quantile(0.99, "
    "sum(rate(loadtester_request_duration_seconds_bucket[1m])) by (le))"
)
QUEUE_DEPTH_QUERY = "dispatcher_queue_depth"
ARRIVAL_RATE_QUERY = "rate(dispatcher_requests_total[1m])"


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


def _load_k8s():
    """Return (AppsV1Api, CustomObjectsApi) or (None, None) if unavailable."""
    try:
        from kubernetes import client, config
        from kubernetes.config.config_exception import ConfigException

        try:
            config.load_incluster_config()
        except ConfigException:
            config.load_kube_config()
        return client.AppsV1Api(), client.CustomObjectsApi()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] kubernetes client unavailable: {exc}", file=sys.stderr)
        return None, None


def read_replicas(apps_api, namespace: str, deployment: str) -> float:
    if apps_api is None:
        return math.nan
    try:
        dep = apps_api.read_namespaced_deployment(deployment, namespace)
        return float(dep.status.ready_replicas or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] read replicas failed: {exc}", file=sys.stderr)
        return math.nan


def _parse_cpu_quantity(value: str) -> float:
    """Convert a Kubernetes CPU quantity (e.g. '250m', '1500000n') to cores."""
    value = value.strip()
    if value.endswith("n"):
        return float(value[:-1]) / 1e9
    if value.endswith("u"):
        return float(value[:-1]) / 1e6
    if value.endswith("m"):
        return float(value[:-1]) / 1e3
    return float(value)


def read_cpu_cores(custom_api, namespace: str, selector: str) -> float:
    if custom_api is None:
        return math.nan
    try:
        metrics = custom_api.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
            label_selector=selector,
        )
        total = 0.0
        for pod in metrics.get("items", []):
            for container in pod.get("containers", []):
                total += _parse_cpu_quantity(container["usage"]["cpu"])
        return total
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] read cpu failed: {exc}", file=sys.stderr)
        return math.nan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect experiment metrics to CSV")
    parser.add_argument("--out", required=True, help="Output CSV path")
    parser.add_argument(
        "--prometheus-url",
        default="http://localhost:9090",
        help="Prometheus base URL (use a port-forward when running locally)",
    )
    parser.add_argument("--namespace", default="inference-system")
    parser.add_argument("--deployment", default="inference")
    parser.add_argument(
        "--selector",
        default="app=inference",
        help="Label selector for inference pods (CPU metrics)",
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
    apps_api, custom_api = _load_k8s()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()

    print(f"Collecting -> {out_path} every {args.interval}s (Ctrl-C to stop)")
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "timestamp", "p99_latency", "e2e_p99", "queue_depth",
            "arrival_rate", "replica_count", "cpu_cores",
        ])
        try:
            while True:
                now = time.time()
                p99 = query_prometheus_scalar(args.prometheus_url, P99_QUERY)
                e2e = query_prometheus_scalar(args.prometheus_url, E2E_P99_QUERY)
                queue = query_prometheus_scalar(args.prometheus_url, QUEUE_DEPTH_QUERY)
                arrival = query_prometheus_scalar(args.prometheus_url, ARRIVAL_RATE_QUERY)
                replicas = read_replicas(apps_api, args.namespace, args.deployment)
                cpu = read_cpu_cores(custom_api, args.namespace, args.selector)
                writer.writerow([
                    round(now, 3), p99, e2e, queue, arrival, replicas, cpu,
                ])
                handle.flush()
                print(
                    f"t={int(now - start):4d}s p99={p99:.3f} e2e={e2e:.3f} "
                    f"queue={queue:.1f} arr={arrival:.1f} "
                    f"replicas={replicas:.0f} cpu={cpu:.3f}"
                )
                if args.duration and (now - start) >= args.duration:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
    print(f"done -> {out_path}")


if __name__ == "__main__":
    main()
