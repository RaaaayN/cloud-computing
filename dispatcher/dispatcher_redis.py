from flask import Flask, request, jsonify
from prometheus_client import start_http_server, Counter, Gauge, Histogram
import threading
import time
import os
import requests

app = Flask(__name__)

INFERENCE_URL = os.getenv("INFERENCE_URL", "http://localhost:6001/")
WORKER_COUNT = int(os.getenv("DISPATCHER_WORKER_COUNT", "8"))
QUEUE_MAX = int(os.getenv("DISPATCHER_QUEUE_MAX", "3"))
FORWARD_TIMEOUT = float(os.getenv("DISPATCHER_FORWARD_TIMEOUT", "5"))

requests_received = Counter("dispatcher_requests_total", "Total requests received")
requests_forwarded = Counter("dispatcher_requests_forwarded", "Total requests forwarded")
requests_dropped = Counter("dispatcher_requests_dropped", "Dropped requests")
queue_size = Gauge("dispatcher_queue_size", "Requests waiting in queue")
request_duration = Histogram(
    "dispatcher_request_duration_seconds",
    "Server-side latency: queue wait + inference",
    buckets=[0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0],
)

inflight = threading.Semaphore(WORKER_COUNT)
waiting = 0
wlock = threading.Lock()


@app.route('/query', methods=['POST'])
def receive_query():
    global waiting
    requests_received.inc()
    start = time.time()

    with wlock:
        if waiting >= QUEUE_MAX:
            requests_dropped.inc()
            return jsonify({"error": "Queue is full"}), 503
        waiting += 1
        queue_size.set(waiting)

    body = request.get_json()
    inflight.acquire()
    with wlock:
        waiting -= 1
        queue_size.set(waiting)
    try:
        res = requests.post(INFERENCE_URL, json={"image": body["image"]}, timeout=FORWARD_TIMEOUT)
        requests_forwarded.inc()
        request_duration.observe(time.time() - start)
        return (res.text, res.status_code, {"Content-Type": "application/json"})
    except Exception as e:
        request_duration.observe(time.time() - start)
        return jsonify({"error": str(e)}), 502
    finally:
        inflight.release()


@app.route('/healthz')
def healthz():
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    print("Dispatcher running on http://localhost:5001")
    start_http_server(8000)
    app.run(port=5001, threaded=True)
