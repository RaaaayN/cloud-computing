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


@app.post("/predict")
def predict(payload: Dict[str, Any]) -> Dict[str, Any]:
    start = time.perf_counter()
    INFERENCE_REQUESTS_TOTAL.inc()

    sleep_seconds = random.uniform(0.1, 0.5)
    time.sleep(sleep_seconds)

    latency = time.perf_counter() - start
    INFERENCE_LATENCY_SECONDS.observe(latency)

    return {
        "ok": True,
        "sleep_seconds": round(sleep_seconds, 4),
        "latency_seconds": round(latency, 4),
        "received": payload,
    }


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}
