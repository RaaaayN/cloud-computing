# heavy_test.py

import requests
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor
from base64 import b64encode

dispatcher_url = "http://localhost:5001/query"  # dispatcher exposed port
image_path = "/app/images/fire_truck.jpeg"


def send_request():
    payload = {"image": image_path}
    try:
        res = requests.post(dispatcher_url, json=payload, timeout=2)
        print(f"✓ {res.status_code}")
    except Exception as e:
        print(f"✗ {e}")


# Read workload as RPS values
with open('workload.txt') as f:
    workload = list(map(int, f.read().split()))

# Simulate each second of load
for second, rps in enumerate(workload, 1):
    print(f"⏱️ Second {second}: Sending {rps} requests")
    with ThreadPoolExecutor(max_workers=rps) as executor:
        for _ in range(rps):
            executor.submit(send_request)
    time.sleep(1)