from flask import Flask, request, jsonify
from prometheus_client import start_http_server, Counter, Gauge
import redis
import uuid
import threading
import time
import base64
import requests

app = Flask(__name__)
r = redis.Redis(host='localhost', port=6379, decode_responses=False)

# Prometheus metrics
requests_received = Counter(
    "dispatcher_requests_total", "Total requests received")
requests_forwarded = Counter(
    "dispatcher_requests_forwarded", "Total requests forwarded")
requests_dropped = Counter(
    "dispatcher_requests_dropped", "Dropped stale requests")
queue_size = Gauge("dispatcher_queue_size", "Current Redis queue size")

# Config
QUEUE_NAME = 'inference_queue'
MAX_WAIT_TIME = 0.5  # seconds
FORWARD_INTERVAL = 0.05  # 50ms
replica_urls = ["http://localhost:6001/"]
replica_index = 0


@app.route('/query', methods=['POST'])
def receive_query():
    # image = request.files.get('image')
    # if not image:
    #     return jsonify({"error": "No image provided"}), 400

    # # Redis stores binary as base64
    # image_data = base64.b64encode(image.read())

    input = request.get_json()
    image_data = input['image']
    timestamp = time.time()
    request_id = str(uuid.uuid4())

    r.rpush(QUEUE_NAME, f"{timestamp}|{request_id}|{image_data}".encode())

    requests_received.inc()
    queue_size.set(r.llen(QUEUE_NAME))

    return jsonify({"message": "Queued"}), 200


def forward_requests():
    global replica_index
    while True:
        queue_size.set(r.llen(QUEUE_NAME))
        item = r.lpop(QUEUE_NAME)
        if item:
            try:
                decoded = item.decode()
                timestamp_str, request_id, image_path = decoded.split("|", 2)
                timestamp = float(timestamp_str)
                now = time.time()

                if now - timestamp > MAX_WAIT_TIME:
                    requests_dropped.inc()
                    continue

                target_url = replica_urls[replica_index]
                # res = requests.post(
                #     target_url, json={"image": image_path}, timeout=1)
                # print(
                #     f"[✓] Forwarded {request_id} to {target_url} → {res.status_code}; Prediction: {res.text}")
                try:
                    res = requests.post(
                        target_url, json={"image": image_path}, timeout=5)
                    print(
                        f"[✓] Forwarded {request_id} to {target_url} → {res.status_code}; Prediction: {res.text}")
                except requests.exceptions.ReadTimeout:
                    print(f"[X] Timeout trying to reach {target_url}")

                requests_forwarded.inc()
                replica_index = (replica_index + 1) % len(replica_urls)

            except Exception as e:
                print(f"[X] Error forwarding: {e}")
        else:
            time.sleep(FORWARD_INTERVAL)


if __name__ == '__main__':
    print("🚀 Redis-based Dispatcher running on http://localhost:5001")
    # Prometheus metrics on http://localhost:8000/metrics
    start_http_server(8000)

    # NEW - multiple threads
    # for _ in range(10):  # Adjust to number of CPUs or desired parallelism
    threading.Thread(target=forward_requests, daemon=True).start()

    app.run(port=5001)
