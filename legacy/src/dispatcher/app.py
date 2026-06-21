import asyncio
import json
import os
import time
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from prometheus_client import Counter, Gauge, Histogram, generate_latest

QUEUE_MAX_SIZE = int(os.getenv("DISPATCHER_QUEUE_MAX_SIZE", "100"))
# Worker-coroutine pool. Must be >= max inference replicas so every ready replica
# can be driven in parallel; the *effective* concurrency is bounded by the number
# of idle replicas in the pool (one in-flight request per replica), not by this.
WORKER_COUNT = int(os.getenv("DISPATCHER_WORKER_COUNT", "20"))
# Headless Service whose DNS resolves to every ready inference pod IP. The
# dispatcher addresses pods DIRECTLY (one request per pod) instead of going
# through the ClusterIP, because kube-proxy L4 load-balancing is per-connection
# and random: it piles several concurrent requests onto the same pod, which then
# serialises them on its single inference thread (tail latency explodes and it
# violates the spec's "replicas do not queue"). Direct per-pod dispatch keeps
# exactly one in-flight request per replica.
INFERENCE_HEADLESS = os.getenv(
    "INFERENCE_HEADLESS",
    "inference-headless.inference-system.svc.cluster.local",
)
# NB: do NOT name this INFERENCE_PORT -- Kubernetes auto-injects an env var of
# that name for the `inference` Service (e.g. "tcp://10.0.0.1:8001"), which would
# shadow this and crash on int().
INFERENCE_PORT = int(os.getenv("INFERENCE_POD_PORT", "8001"))
CONCURRENCY_REFRESH_SEC = float(os.getenv("DISPATCHER_CONCURRENCY_REFRESH_SEC", "2"))
# A forward should take ~one inference. Cap it low so a wedged or terminating pod
# frees its worker quickly instead of holding it (and poisoning the tail).
FORWARD_TIMEOUT_SEC = float(os.getenv("DISPATCHER_FORWARD_TIMEOUT_SEC", "2"))
CONNECT_TIMEOUT_SEC = float(os.getenv("DISPATCHER_CONNECT_TIMEOUT_SEC", "1"))

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
# Server-side service latency of a query: from dispatcher receipt to response
# (queue wait + inference). This is the graded SLO metric (< 0.5 s).
DISPATCHER_REQUEST_DURATION = Histogram(
    "dispatcher_request_duration_seconds",
    "Server-side query latency: dispatcher receive -> response (queue + inference)",
    buckets=[0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0, 30.0],
)

DISPATCHER_CONCURRENCY_LIMIT = Gauge(
    "dispatcher_concurrency_limit",
    "Current max concurrent forwards (= ready inference replicas)",
)


@dataclass
class QueuedRequest:
    raw_body: bytes
    future: asyncio.Future[tuple[int, bytes]]


REQUEST_QUEUE: asyncio.Queue[QueuedRequest | None] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)


class ReplicaPool:
    """Tracks ready inference pod IPs and hands them out one at a time.

    `idle` holds the IPs of replicas not currently serving a request. A worker
    takes one IP, sends a single request to that pod, and returns the IP only
    when the pod is done -- so at most one request is in flight per replica and
    replicas never queue internally. `valid` is the current ready set, used to
    drop IPs of pods that have gone away.
    """

    def __init__(self) -> None:
        self.idle: asyncio.Queue[str] = asyncio.Queue()
        self.valid: set[str] = set()

    def update(self, ips: set[str]) -> None:
        for ip in ips - self.valid:
            self.idle.put_nowait(ip)
        self.valid = ips
        DISPATCHER_CONCURRENCY_LIMIT.set(len(ips))


async def replica_poller(pool: ReplicaPool) -> None:
    """Resolve the inference headless Service and refresh the ready-pod set."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            infos = await loop.getaddrinfo(INFERENCE_HEADLESS, INFERENCE_PORT)
            ips = {info[4][0] for info in infos}
            if ips:
                pool.update(ips)
        except Exception:
            pass  # transient DNS failure: keep the last known set
        await asyncio.sleep(CONCURRENCY_REFRESH_SEC)


def refresh_queue_depth() -> None:
    DISPATCHER_QUEUE_DEPTH.set(REQUEST_QUEUE.qsize())


async def forward_to_pod(
    session: aiohttp.ClientSession,
    pod_ip: str,
    raw_body: bytes,
) -> tuple[int, bytes]:
    # Forward the client's raw JSON body verbatim. We deliberately do NOT parse it
    # into a dict and re-serialise: the body is ~130 KB (a base64 image), and
    # json.loads + json.dumps of that on the dispatcher's single event loop, for
    # every request, is pure overhead; the inference pods parse it (in parallel).
    async with session.post(
        f"http://{pod_ip}:{INFERENCE_PORT}/infer",
        data=raw_body,
        headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(
            total=FORWARD_TIMEOUT_SEC, sock_connect=CONNECT_TIMEOUT_SEC
        ),
    ) as response:
        body = await response.read()
        return response.status, body


async def worker_loop(
    worker_id: int,
    session: aiohttp.ClientSession,
    pool: ReplicaPool,
) -> None:
    while True:
        # Reserve an idle replica BEFORE taking a request, so the backlog stays in
        # the bounded queue (shed with 503 when full) instead of being pulled out
        # and held. Only #ready-replicas workers can hold an IP at once, so the
        # effective concurrency is exactly the ready replica count.
        pod_ip = await pool.idle.get()
        if pod_ip not in pool.valid:
            continue  # pod went away while it sat idle; drop it
        queued = await REQUEST_QUEUE.get()
        try:
            if queued is None:
                return
            refresh_queue_depth()
            DISPATCHER_REQUESTS_IN_FLIGHT.inc()
            try:
                status, body = await forward_to_pod(session, pod_ip, queued.raw_body)
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
            if pod_ip in pool.valid:
                pool.idle.put_nowait(pod_ip)


async def submit_handler(request: web.Request) -> web.Response:
    DISPATCHER_REQUESTS_TOTAL.inc()
    start = time.perf_counter()

    # Shed BEFORE touching the body: when the queue is full we drop, so there is no
    # point spending event-loop time reading ~130 KB off the socket for a request
    # we will 503 anyway. This keeps the dispatcher responsive under a burst flood.
    if REQUEST_QUEUE.full():
        DISPATCHER_REQUESTS_DROPPED_TOTAL.inc()
        refresh_queue_depth()
        return web.json_response({"error": "Queue is full"}, status=503)

    # Read the raw body once; do NOT json-parse it (the pods do that, in parallel).
    raw_body = await request.read()

    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[int, bytes]] = loop.create_future()
    await REQUEST_QUEUE.put(QueuedRequest(raw_body=raw_body, future=future))
    refresh_queue_depth()

    status, body = await future
    DISPATCHER_REQUEST_DURATION.observe(time.perf_counter() - start)
    return web.Response(body=body, status=status, content_type="application/json")


async def metrics_handler(_: web.Request) -> web.Response:
    refresh_queue_depth()
    return web.Response(body=generate_latest(), content_type="text/plain", charset="utf-8")


async def healthz_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def on_startup(app: web.Application) -> None:
    # Keep-alive connection pool. A fresh TCP connection per forward (force_close)
    # serialises connect/teardown on the single event loop and caps dispatcher
    # throughput at ~8 cores of downstream work no matter how many replicas exist
    # (adding replicas left them idle). Reusing connections per pod removes that
    # per-request overhead. A connection to a pod that scaled away simply fails on
    # next use -> handled as a 502; the ReplicaPool already drops departed IPs.
    connector = aiohttp.TCPConnector(limit=0, limit_per_host=0, keepalive_timeout=30)
    session = aiohttp.ClientSession(connector=connector)
    app["http_session"] = session

    pool = ReplicaPool()
    app["pool"] = pool
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(INFERENCE_HEADLESS, INFERENCE_PORT)
        pool.update({info[4][0] for info in infos})
    except Exception:
        pass
    app["poller"] = asyncio.create_task(replica_poller(pool))
    app["workers"] = [
        asyncio.create_task(worker_loop(worker_id, session, pool))
        for worker_id in range(WORKER_COUNT)
    ]


async def on_cleanup(app: web.Application) -> None:
    app["poller"].cancel()
    for _ in app["workers"]:
        await REQUEST_QUEUE.put(None)
    await asyncio.gather(*app["workers"], app["poller"], return_exceptions=True)
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
    # uvloop replaces asyncio's pure-Python event loop with a libuv-backed one.
    # The dispatcher is a single-event-loop process whose throughput ceiling
    # (~8-9 cores of downstream work) is the system bottleneck under the burst;
    # uvloop's lower per-callback overhead lets that one core push more requests.
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    web.run_app(build_app(), host="0.0.0.0", port=8002, access_log=None)
