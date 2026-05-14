import random
import time
from typing import Any, Dict

from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

app = FastAPI(title="Mock Inference Service")

INFERENCE_LATENCY_SECONDS = Histogram(
    "inference_latency_seconds",
    "Latency of mock inference requests in seconds",
)
INFERENCE_REQUESTS_TOTAL = Counter(
    "inference_requests_total",
    "Total number of inference requests",
)


def _cpu_spin(milliseconds: int) -> None:
    # busy-wait to raise CPU for HPA demos (sleep alone barely moves CPU%)
    if milliseconds <= 0:
        return
    deadline = time.perf_counter() + milliseconds / 1000.0
    x = 0
    while time.perf_counter() < deadline:
        x += 1  # noqa: B007


@app.post("/predict")
def predict(payload: Dict[str, Any]) -> Dict[str, Any]:
    start = time.perf_counter()
    INFERENCE_REQUESTS_TOTAL.inc()

    sleep_seconds = random.uniform(0.1, 0.5)  # mock inference delay
    time.sleep(sleep_seconds)

    spin_raw = payload.get("cpu_spin_ms")
    spin_ms = 0
    if isinstance(spin_raw, (int, float)):
        spin_ms = int(spin_raw)
    elif isinstance(spin_raw, str) and spin_raw.isdigit():
        spin_ms = int(spin_raw)
    spin_ms = max(0, min(spin_ms, 2000))
    if spin_ms:
        _cpu_spin(spin_ms)

    latency = time.perf_counter() - start
    INFERENCE_LATENCY_SECONDS.observe(latency)

    return {
        "ok": True,
        "sleep_seconds": round(sleep_seconds, 4),
        "cpu_spin_ms": spin_ms,
        "latency_seconds": round(latency, 4),
        "received": payload,
    }


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}
