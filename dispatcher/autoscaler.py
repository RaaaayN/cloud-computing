import requests
import time
import subprocess

PROM_URL = "http://localhost:9090"
DEPLOYMENT_NAME = "tu-cloud-project"
NAMESPACE = "default"
MIN_REPLICAS = 1
MAX_REPLICAS = 10
SCALE_INTERVAL = 15  # seconds

# === Helper: Query Prometheus ===


def query_prometheus(promql):
    try:
        res = requests.get(f"{PROM_URL}/api/v1/query",
                           params={"query": promql})
        result = res.json()['data']['result']
        if not result:
            print(f"[!] No result for: {promql}")
            return None
        return float(result[0]['value'][1])
    except Exception as e:
        print(f"[!] Prometheus query error for '{promql}': {e}")
        return None


# === Scaling Logic ===


def compute_target_replicas(p99_latency, queue_size, current_replicas):
    if p99_latency is None or queue_size is None:
        return current_replicas

    # Simple scaling logic
    if p99_latency > 0.5 or queue_size > 10:
        return min(current_replicas + 1, MAX_REPLICAS)
    elif p99_latency < 0.2 and queue_size < 3:
        return max(current_replicas - 1, MIN_REPLICAS)
    return current_replicas

# === Get Current Replica Count ===


def get_current_replicas():
    output = subprocess.check_output(
        ["kubectl", "get", "deployment", DEPLOYMENT_NAME,
            "-n", NAMESPACE, "-o", "jsonpath={.spec.replicas}"]
    )
    return int(output.decode())

# === Apply Scaling ===


def scale_to_replicas(n):
    subprocess.run(
        ["kubectl", "scale", "deployment", DEPLOYMENT_NAME,
            "-n", NAMESPACE, f"--replicas={n}"]
    )
    print(f"[✓] Scaled to {n} replicas")


# === Main Loop ===
while True:
    try:
        p99_latency = query_prometheus("""
    histogram_quantile(0.99, rate(inference_latency_seconds_bucket[1m]))
""")

        queue_size = query_prometheus("dispatcher_queue_size")

        print(f"P99 latency: {p99_latency:.2f}s" if p99_latency is not None else "P99 latency: N/A",
              "| Queue size:", int(queue_size) if queue_size is not None else "N/A")

        current_replicas = get_current_replicas()
        new_replicas = compute_target_replicas(
            p99_latency, queue_size, current_replicas)

        if new_replicas != current_replicas:
            scale_to_replicas(new_replicas)
        else:
            print("No scaling needed")

    except Exception as e:
        print(f"[!] Error: {e}")

    time.sleep(SCALE_INTERVAL)
