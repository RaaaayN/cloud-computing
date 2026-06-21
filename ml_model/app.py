from flask import Flask, request, jsonify
import base64
from model import get_prediction
from prometheus_client import Counter, Histogram, start_http_server

# Start Prometheus metrics server on port 8001
start_http_server(8001)

# Prometheus metrics
requests_total = Counter("inference_requests_total",
                         "Total inference requests")
inference_latency = Histogram(
    "inference_latency_seconds", "Latency of inference")

# Initialize the Flask app
app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
@inference_latency.time()
def index():
    requests_total.inc()
    input = request.get_json()
    if not input or 'image' not in input:
        return jsonify({"error": "Missing 'image'"}), 400

    try:
        img_bytes = base64.b64decode(input['image'])
        result = get_prediction(img_bytes)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6001)
