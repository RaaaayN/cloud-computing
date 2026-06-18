"""Overlay the 3 experiment runs (custom / HPA70 / HPA90) on two figures.

Produces the slide-17 time-series comparison:
    - p99 latency vs time
    - CPU cores vs time

Usage:
    python experiments/plot.py custom.csv hpa70.csv hpa90.csv
    python experiments/plot.py custom.csv hpa70.csv hpa90.csv --out-prefix figs/run1

Each input CSV is produced by collect.py:
    timestamp, p99_latency, replica_count, cpu_cores
"""
import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt

SLO_SECONDS = 0.5


def load_series(path: str):
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(row)
    if not rows:
        raise ValueError(f"{path} is empty")
    t0 = float(rows[0]["timestamp"])

    def col(name):
        out = []
        for row in rows:
            try:
                out.append(float(row[name]))
            except (ValueError, KeyError):
                out.append(float("nan"))
        return out

    elapsed = [float(r["timestamp"]) - t0 for r in rows]
    return {
        "label": Path(path).stem,
        "t": elapsed,
        "p99": col("p99_latency"),
        "e2e_p99": col("e2e_p99"),
        "queue_depth": col("queue_depth"),
        "cpu": col("cpu_cores"),
        "replicas": col("replica_count"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot experiment comparison")
    parser.add_argument("csv", nargs="+", help="One or more collect.py CSV files")
    parser.add_argument("--out-prefix", default="comparison")
    return parser.parse_args()


def _save_timeseries(series, key, ylabel, title, out_path, slo=False):
    plt.figure(figsize=(10, 5))
    for s in series:
        plt.plot(s["t"], s[key], marker=".", label=s["label"])
    if slo:
        plt.axhline(SLO_SECONDS, color="red", linestyle="--", label=f"SLO {SLO_SECONDS}s")
    plt.xlabel("time (s)")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"wrote {out_path}")


def main() -> None:
    args = parse_args()
    series = [load_series(path) for path in args.csv]
    p = args.out_prefix

    # End-to-end client p99 is the real SLO metric (includes queue wait).
    _save_timeseries(series, "e2e_p99", "end-to-end p99 latency (s)",
                     "End-to-end client p99 vs time", f"{p}_e2e_p99.png", slo=True)
    _save_timeseries(series, "p99", "server p99 inference latency (s)",
                     "Server-side inference p99 vs time", f"{p}_p99.png", slo=True)
    _save_timeseries(series, "cpu", "CPU cores used (inference pods)",
                     "CPU cores vs time", f"{p}_cpu.png")
    _save_timeseries(series, "replicas", "inference replicas",
                     "Replica count vs time", f"{p}_replicas.png")
    _save_timeseries(series, "queue_depth", "dispatcher queue depth",
                     "Dispatcher queue depth vs time", f"{p}_queue.png")


if __name__ == "__main__":
    main()
