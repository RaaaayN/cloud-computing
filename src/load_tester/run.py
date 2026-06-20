import argparse
import asyncio
import csv
import os
import random
import time
from pathlib import Path

import httpx
from aiohttp import web
from prometheus_client import Counter, Histogram, generate_latest

from load_tester.images import build_submit_payload, fetch_samples

LOADTESTER_REQUESTS_TOTAL = Counter(
    "loadtester_requests_total",
    "Total load tester requests",
    ["status"],
)
LOADTESTER_REQUEST_DURATION_SECONDS = Histogram(
    "loadtester_request_duration_seconds",
    "End-to-end client latency in seconds",
    buckets=[0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0, 30.0],
)
LOADTESTER_DROPPED_TOTAL = Counter(
    "loadtester_dropped_total",
    "Requests dropped client-side because the in-flight cap was reached",
)

METRICS_PORT = int(os.getenv("LOADTESTER_METRICS_PORT", "8003"))
# Cap on concurrent in-flight requests. Without a cap, a burst that saturates the
# dispatcher queue makes responses pile up, exhausts the httpx connection pool and
# stalls the scheduling loop -> the load tester stops emitting traffic mid-run.
MAX_INFLIGHT = int(os.getenv("LOADTESTER_MAX_INFLIGHT", "200"))


def target_rps(t: float, dur: float, base: float, peak: float) -> float:
    """Triangle wave RPS profile: base -> peak -> base over duration."""
    half = dur / 2
    if t <= half:
        return base + (peak - base) * (t / half)
    return base + (peak - base) * ((dur - t) / half)


def load_workload(path: str) -> list[float]:
    """Read a whitespace-separated trace of per-second RPS values."""
    text = Path(path).read_text(encoding="utf-8")
    values = [float(tok) for tok in text.split()]
    if not values:
        raise ValueError(f"workload file {path} is empty")
    return values


def workload_rps(t: float, trace: list[float]) -> float:
    """RPS for elapsed second t from a replayed trace (clamped to the end)."""
    return trace[min(int(t), len(trace) - 1)]


async def metrics_handler(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), content_type="text/plain", charset="utf-8")


async def start_metrics_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/metrics", metrics_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


async def send(
    client: httpx.AsyncClient,
    target: str,
    image_b64: str,
    writer: csv.writer,
    csv_lock: asyncio.Lock,
) -> None:
    payload = build_submit_payload(image_b64)
    t0 = time.perf_counter()
    status_label = "error"
    status_code = -1
    try:
        response = await client.post(
            f"{target.rstrip('/')}/submit",
            json=payload,
            timeout=15.0,
        )
        status_code = response.status_code
        status_label = str(status_code)
        LOADTESTER_REQUEST_DURATION_SECONDS.observe(time.perf_counter() - t0)
    except Exception:
        status_label = "error"
    finally:
        LOADTESTER_REQUESTS_TOTAL.labels(status=status_label).inc()
        latency = round(time.perf_counter() - t0, 4)
        async with csv_lock:
            writer.writerow([round(time.time(), 3), status_code, latency])


async def run(
    target: str,
    duration: float,
    base: float,
    peak: float,
    out: str,
    metrics_port: int,
    workload: str | None = None,
    max_inflight: int = MAX_INFLIGHT,
) -> None:
    trace = load_workload(workload) if workload else None
    if duration <= 0:
        duration = float(len(trace)) if trace is not None else 300.0
    images = await fetch_samples()
    metrics_runner = await start_metrics_server(metrics_port)

    output_path = Path(out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    csv_lock = asyncio.Lock()

    try:
        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "status", "latency_seconds"])

            limits = httpx.Limits(
                max_connections=max_inflight,
                max_keepalive_connections=max_inflight,
            )
            async with httpx.AsyncClient(limits=limits) as client:
                start = time.perf_counter()
                tasks: list[asyncio.Task[None]] = []
                inflight = 0

                async def tracked(image_b64: str) -> None:
                    nonlocal inflight
                    try:
                        await send(client, target, image_b64, writer, csv_lock)
                    finally:
                        inflight -= 1

                while True:
                    elapsed = time.perf_counter() - start
                    if elapsed >= duration:
                        break
                    if trace is not None:
                        rps = workload_rps(elapsed, trace)
                    else:
                        rps = target_rps(elapsed, duration, base, peak)
                    interval = 1.0 / rps if rps > 0 else 1.0

                    # When at the in-flight cap, drop client-side (like the
                    # dispatcher does on a full queue) and keep pacing the
                    # workload instead of blocking and stalling the run.
                    if inflight >= max_inflight:
                        LOADTESTER_DROPPED_TOTAL.inc()
                    else:
                        inflight += 1
                        tasks.append(
                            asyncio.create_task(tracked(random.choice(images)))
                        )

                    await asyncio.sleep(interval * random.uniform(0.8, 1.2))
                    if len(tasks) > 500:
                        tasks = [task for task in tasks if not task.done()]
                    if int(elapsed) % 10 == 0 and elapsed - int(elapsed) < 0.1:
                        print(f"[t={int(elapsed):4d}s] rps={rps:.1f} inflight={inflight}")
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await metrics_runner.cleanup()

    print(f"done -> {out}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load tester for dispatcher /submit")
    parser.add_argument("--target", required=True, help="Dispatcher base URL")
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Seconds to run (0 = workload length if --workload, else 300)",
    )
    parser.add_argument("--base", type=float, default=1.0, help="Minimum RPS (triangle)")
    parser.add_argument("--peak", type=float, default=20.0, help="Peak RPS (triangle)")
    parser.add_argument(
        "--workload",
        default=None,
        help="Path to a whitespace-separated per-second RPS trace to replay "
        "(overrides the triangle base/peak profile)",
    )
    parser.add_argument("--out", default="results.csv")
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=METRICS_PORT,
        help="HTTP port for Prometheus /metrics",
    )
    parser.add_argument(
        "--max-inflight",
        type=int,
        default=MAX_INFLIGHT,
        help="Max concurrent in-flight requests before dropping client-side",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        run(
            target=args.target,
            duration=args.duration,
            base=args.base,
            peak=args.peak,
            out=args.out,
            metrics_port=args.metrics_port,
            workload=args.workload,
            max_inflight=args.max_inflight,
        )
    )


if __name__ == "__main__":
    main()
