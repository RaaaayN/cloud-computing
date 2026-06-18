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
        "cpu": col("cpu_cores"),
        "replicas": col("replica_count"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot experiment comparison")
    parser.add_argument("csv", nargs="+", help="One or more collect.py CSV files")
    parser.add_argument("--out-prefix", default="comparison")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    series = [load_series(path) for path in args.csv]

    # Figure 1 — p99 latency vs time
    plt.figure(figsize=(10, 5))
    for s in series:
        plt.plot(s["t"], s["p99"], marker=".", label=s["label"])
    plt.axhline(SLO_SECONDS, color="red", linestyle="--", label=f"SLO {SLO_SECONDS}s")
    plt.xlabel("time (s)")
    plt.ylabel("p99 inference latency (s)")
    plt.title("p99 latency vs time")
    plt.legend()
    plt.grid(True, alpha=0.3)
    p99_path = f"{args.out_prefix}_p99.png"
    plt.tight_layout()
    plt.savefig(p99_path, dpi=150)
    print(f"wrote {p99_path}")

    # Figure 2 — CPU cores vs time
    plt.figure(figsize=(10, 5))
    for s in series:
        plt.plot(s["t"], s["cpu"], marker=".", label=s["label"])
    plt.xlabel("time (s)")
    plt.ylabel("CPU cores used (inference pods)")
    plt.title("CPU cores vs time")
    plt.legend()
    plt.grid(True, alpha=0.3)
    cpu_path = f"{args.out_prefix}_cpu.png"
    plt.tight_layout()
    plt.savefig(cpu_path, dpi=150)
    print(f"wrote {cpu_path}")


if __name__ == "__main__":
    main()
