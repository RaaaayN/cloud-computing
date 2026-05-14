import asyncio
import os
from typing import Any, Dict

import requests
from fastapi import FastAPI, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from starlette.responses import Response

app = FastAPI(title="Inference Dispatcher")

BACKEND_URL = "http://inference-service:8000/predict"
REQUEST_TIMEOUT_SECONDS = 5
# parallel forwarders; 1 worker serializes backend and kills CPU-based HPA signal
DISPATCHER_CONCURRENCY = max(1, int(os.environ.get("DISPATCHER_CONCURRENCY", "16")))
REQUEST_QUEUE: asyncio.Queue = asyncio.Queue()
DISPATCHER_QUEUE_DEPTH = Gauge(
    "dispatcher_queue_depth",
    "Current number of requests waiting in dispatcher queue",
)


async def queue_worker() -> None:
    while True:
        payload, response_future = await REQUEST_QUEUE.get()
        DISPATCHER_QUEUE_DEPTH.set(REQUEST_QUEUE.qsize())
        try:
            backend_response = await asyncio.to_thread(
                requests.post,
                BACKEND_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            backend_response.raise_for_status()
            response_future.set_result(backend_response.json())
        except Exception as exc:
            response_future.set_exception(exc)
        finally:
            REQUEST_QUEUE.task_done()
            DISPATCHER_QUEUE_DEPTH.set(REQUEST_QUEUE.qsize())


@app.on_event("startup")
async def startup_event() -> None:
    for _ in range(DISPATCHER_CONCURRENCY):
        asyncio.create_task(queue_worker())


@app.post("/predict")
async def predict(payload: Dict[str, Any]) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    response_future: asyncio.Future = loop.create_future()

    await REQUEST_QUEUE.put((payload, response_future))  # queue for incoming reqs
    DISPATCHER_QUEUE_DEPTH.set(REQUEST_QUEUE.qsize())

    try:
        backend_result = await response_future
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Backend request failed: {exc}") from exc

    return {
        "queued_depth_after_enqueue": REQUEST_QUEUE.qsize(),
        "backend_result": backend_result,
    }


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "dispatcher",
        "message": "Use POST /predict to send inference requests.",
        "available_paths": ["/predict", "/metrics", "/healthz", "/docs"],
    }


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}
