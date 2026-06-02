import asyncio
import json
import os
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

QUEUE_MAX_SIZE = int(os.getenv("DISPATCHER_QUEUE_MAX_SIZE", "100"))

REQUEST_QUEUE: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)

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
    "Total number of requests accepted by dispatcher",
)
DISPATCHER_REQUESTS_DROPPED_TOTAL = Counter(
    "dispatcher_requests_dropped_total",
    "Total number of requests dropped by dispatcher",
)


def refresh_queue_depth() -> None:
    DISPATCHER_QUEUE_DEPTH.set(REQUEST_QUEUE.qsize())


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

    DISPATCHER_REQUESTS_IN_FLIGHT.inc()
    await REQUEST_QUEUE.put(payload["data"])
    refresh_queue_depth()
    DISPATCHER_REQUESTS_COMPLETED_TOTAL.inc()
    DISPATCHER_REQUESTS_IN_FLIGHT.dec()

    return web.json_response(
        {
            "status": "accepted",
            "message": "Request queued (stub mode, forwarding not implemented yet).",
            "queue_depth": REQUEST_QUEUE.qsize(),
        },
        status=202,
    )


async def metrics_handler(_: web.Request) -> web.Response:
    refresh_queue_depth()
    return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)


async def healthz_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def build_app() -> web.Application:
    app = web.Application()
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
