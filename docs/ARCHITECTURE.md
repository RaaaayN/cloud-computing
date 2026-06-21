# System architecture

This document describes the **implemented** architecture of the *Elastic ML Inference Serving* project: request flow, metrics, and scaling loop.

> 📄 The complete design, parameter rationale and results are in
> [`experiments/results/REPORT.md`](../experiments/results/REPORT.md).

---

## 1. Overview

The system follows the brief's model:

**Load tester → Dispatcher (queue) → Inference (N replicas) ← Autoscaler ← Prometheus**

```mermaid
sequenceDiagram
  participant LT as LoadTester
  participant D as Dispatcher
  participant Q as Queue
  participant W as Workers
  participant I as Inference
  participant P as Prometheus
  participant A as Autoscaler

  LT->>D: POST /submit JSON base64
  D->>Q: enqueue if space
  W->>Q: dequeue
  W->>I: POST /infer
  I-->>W: labels JSON
  W-->>D: response
  D-->>LT: E2E response

  LT->>P: scrape /metrics :8003
  D->>P: scrape /metrics :8002
  I->>P: scrape /metrics :8001
  A->>P: PromQL every 15s
  A->>I: patch Deployment replicas
```

---

## 2. Unified API contract

All services use the same image payload format:

```json
{"data": "<base64 JPEG>"}
```

| Service | Endpoint | Method | Response |
|---------|----------|--------|----------|
| Inference | `/infer` | POST | `["label1", ...]` (top-5 ImageNet) |
| Dispatcher | `/submit` | POST | Synchronous proxy to inference |
| Load tester | — | — | Client of `/submit` |

**Shared utility endpoints:**

| Endpoint | Role |
|----------|------|
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness (inference only) |
| `GET /metrics` | Prometheus text exposition |

---

## 3. Components

### 3.1 Inference (`model_server.py`)

- **ResNet18** model, weights **baked into the image** (fast startup).
- Threads pinned to 1 (`torch.set_num_threads(1)` + `OMP/MKL/OpenBLAS/NumExpr=1`) so a pod cannot oversubscribe its 1-CPU quota.
- **Sequential** processing: one inference at a time per pod (no internal pool).
- Metrics: `inference_requests_total`, `inference_duration_seconds`.

### 3.2 Dispatcher (`src/dispatcher/app.py`)

- **Short bounded queue** (`asyncio.Queue`, size 3); **503** when full (load shedding).
- **Headless per-pod dispatch**: forwards to ready pod IPs directly, **one in-flight request per pod** (not the ClusterIP Service, whose random LB piles requests onto one pod). Worker pool `DISPATCHER_WORKER_COUNT=20`; effective concurrency = ready replicas.
- The `/submit` handler waits for the inference response → measures **server-side** latency (`dispatcher_request_duration_seconds`).

See [DISPATCHER.md](DISPATCHER.md).

### 3.3 Load tester (`src/load_tester/`)

- Origin: `load-tester` branch (Sakshi's script), refactored for the `/submit` API.
- **Triangle** load profile: RPS rises from `base` to `peak` then falls over `duration` seconds.
- Images: ImageNet samples downloaded and base64-encoded.
- **CSV** export (`timestamp, status, latency_seconds`) + Prometheus metrics.
- `/metrics` server on port **8003** while running.

See [LOAD_TESTER.md](LOAD_TESTER.md).

### 3.4 Autoscaler (`src/autoscaler/`)

- **MAPE** loop every **15 s**.
- Reads Prometheus: `dispatcher_queue_depth`, server-side p99 `dispatcher_request_duration_seconds`, `rate(dispatcher_requests_total[30s])`.
- **Queue + SLO** policy (`QueueSloPolicy`): fast scale-up after 2 pressure cycles, scale-down with cooldown. `min/max = 1/3`.
- Patches `Deployment/inference` via the Kubernetes client (dedicated RBAC).
- `--dry-run` mode: log decisions without patching (K8s manifest default).

See [AUTOSCALER.md](AUTOSCALER.md).

### 3.5 Prometheus

- Scrape interval **15 s** (aligned with autoscaler / HPA).
- Jobs in `k8s/prometheus/configmap.yaml`:
  - `inference` :8001
  - `dispatcher` :8002
  - `loadtester` :8003

---

## 4. Key metrics

### Dispatcher (scaling signals)

| Metric | Type | Autoscaler use |
|--------|------|----------------|
| `dispatcher_queue_depth` | Gauge | Backlog, scale-up |
| `dispatcher_requests_total` | Counter | Arrival rate λ |
| `dispatcher_requests_in_flight` | Gauge | In-flight load |
| `dispatcher_requests_completed_total` | Counter | Throughput |
| `dispatcher_requests_dropped_total` | Counter | Overload (503) |

### Server-side SLO (graded)

| Metric | Type | Use |
|--------|------|-----|
| `dispatcher_request_duration_seconds` | Histogram | **p99 vs 0.5 s SLO** (queue wait + inference) |
| `inference_duration_seconds` | Histogram | Inference time alone (diagnostic) |

### Load tester (client-side, reports)

| Metric | Type | Use |
|--------|------|-----|
| `loadtester_request_duration_seconds` | Histogram | client p99 |
| `loadtester_requests_total{status}` | Counter | Success/error rate |

**Reference PromQL:**

```promql
# Server-side p99 latency (the graded SLO metric)
histogram_quantile(0.99, sum(rate(dispatcher_request_duration_seconds_bucket[1m])) by (le))

# Arrival rate (leading demand signal)
rate(dispatcher_requests_total[30s])
```

---

## 5. Design principles

| Principle | Rationale |
|-----------|-----------|
| Single queue at dispatcher | Only observable backlog point; matches the brief |
| One in-flight request per pod (headless) | Enforces "replicas do not queue, one inference at a time" (slide 21) |
| 15 s decision interval | Fair comparison with HPA |
| Bounded delta per cycle (5) | Reach the cap in one cycle on a burst, still bounded |
| Scale-down hysteresis | 6 stable cycles (~90 s), ≪ HPA's 5-min default |

---

## 6. HPA vs custom autoscaler

| | HPA (CPU 70/90%) | Custom autoscaler |
|--|------------------|-------------------|
| Signal | Average CPU utilization | Queue + p99 latency |
| Burst response | Delayed (CPU rises after queue builds) | Leading indicator via `queue_depth` |
| Formula | `ceil(replicas × CPU / target)` | Little's Law + SLO guardrail |
| Interval | ~15 s | 15 s (configurable) |

---

## 7. Source files

| Path | Responsibility |
|------|----------------|
| `model_server.py` | ResNet18 inference |
| `src/dispatcher/app.py` | Queue, workers, forward |
| `src/load_tester/run.py` | Load generation + metrics |
| `src/load_tester/images.py` | Base64 samples |
| `src/autoscaler/controller.py` | MAPE loop |
| `src/autoscaler/policies/queue_slo_policy.py` | Scaling decisions |
| `src/autoscaler/prometheus_client.py` | PromQL queries |
| `src/autoscaler/k8s_client.py` | Deployment patch |
