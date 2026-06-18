import asyncio
import json
import os
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from prometheus_client import Counter, Gauge, generate_latest

QUEUE_MAX_SIZE = int(os.getenv("DISPATCHER_QUEUE_MAX_SIZE", "100"))
INFERENCE_URL = os.getenv(
    "INFERENCE_URL",
    "http://inference.inference-system.svc.cluster.local:8001",
)
WORKER_COUNT = int(os.getenv("DISPATCHER_WORKER_COUNT", "4"))

DISPATCHER_QUEUE_DEPTH = Gauge(
    "dispatcher_queue_depth",
    "Number of requests currently waiting in dispatcher queue",
)
DISPATCHER_REQUESTS_IN_FLIGHT = Gauge(
    "dispatcher_requests_in_flight",
    "Number of requests currently processed by dispatcher",
)
DISPATCHER_REQUESTS_TOTAL = Counter(
    "dispatcher_requests_total",
    "Total number of requests received by dispatcher",
)
DISPATCHER_REQUESTS_COMPLETED_TOTAL = Counter(
    "dispatcher_requests_completed_total",
    "Total number of requests completed successfully by dispatcher",
)
DISPATCHER_REQUESTS_DROPPED_TOTAL = Counter(
    "dispatcher_requests_dropped_total",
    "Total number of requests dropped by dispatcher",
)


@dataclass
class QueuedRequest:
    data: str
    future: asyncio.Future[tuple[int, bytes]]


REQUEST_QUEUE: asyncio.Queue[QueuedRequest | None] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)


def refresh_queue_depth() -> None:
    DISPATCHER_QUEUE_DEPTH.set(REQUEST_QUEUE.qsize())


async def forward_to_inference(
    session: aiohttp.ClientSession,
    image_data: str,
) -> tuple[int, bytes]:
    async with session.post(
        f"{INFERENCE_URL.rstrip('/')}/infer",
        json={"data": image_data},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        body = await response.read()
        return response.status, body


async def worker_loop(worker_id: int, session: aiohttp.ClientSession) -> None:
    while True:
        queued = await REQUEST_QUEUE.get()
        try:
            if queued is None:
                return
            refresh_queue_depth()
            DISPATCHER_REQUESTS_IN_FLIGHT.inc()
            try:
                status, body = await forward_to_inference(session, queued.data)
                if not queued.future.done():
                    queued.future.set_result((status, body))
                if status == 200:
                    DISPATCHER_REQUESTS_COMPLETED_TOTAL.inc()
            except Exception as exc:
                if not queued.future.done():
                    error_body = json.dumps({"error": str(exc)}).encode("utf-8")
                    queued.future.set_result((502, error_body))
            finally:
                DISPATCHER_REQUESTS_IN_FLIGHT.dec()
                refresh_queue_depth()
        finally:
            REQUEST_QUEUE.task_done()


async def submit_handler(request: web.Request) -> web.Response:
    DISPATCHER_REQUESTS_TOTAL.inc()

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON payload"}, status=400)

    if "data" not in payload:
        return web.json_response({"error": "Missing required field: data"}, status=400)

    if REQUEST_QUEUE.full():
        DISPATCHER_REQUESTS_DROPPED_TOTAL.inc()
        refresh_queue_depth()
        return web.json_response({"error": "Queue is full"}, status=503)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[int, bytes]] = loop.create_future()
    await REQUEST_QUEUE.put(QueuedRequest(data=payload["data"], future=future))
    refresh_queue_depth()

    status, body = await future
    return web.Response(body=body, status=status, content_type="application/json")


async def metrics_handler(_: web.Request) -> web.Response:
    refresh_queue_depth()
    return web.Response(body=generate_latest(), content_type="text/plain", charset="utf-8")


async def healthz_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def on_startup(app: web.Application) -> None:
    session = aiohttp.ClientSession()
    app["http_session"] = session
    app["workers"] = [
        asyncio.create_task(worker_loop(worker_id, session))
        for worker_id in range(WORKER_COUNT)
    ]


async def on_cleanup(app: web.Application) -> None:
    for _ in app["workers"]:
        await REQUEST_QUEUE.put(None)
    await asyncio.gather(*app["workers"], return_exceptions=True)
    await app["http_session"].close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes(
        [
            web.post("/submit", submit_handler),
            web.get("/metrics", metrics_handler),
            web.get("/healthz", healthz_handler),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=8002, access_log=None)
