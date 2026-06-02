# Custom Autoscaler — Implementation Documentation

This document describes a recommended implementation for the **Elastic Scaling Algorithm (The Brain)** part of the *Elastic ML Inference Serving* project: an autonomous controller that dynamically adjusts the number of ResNet18 inference replicas every **15 seconds**, based on latency and queueing rather than CPU utilization alone.

**Primary SLO:** server-side latency **< 0.5 s** (p99 used as the reference metric for comparison graphs).

**Baseline:** Kubernetes Horizontal Pod Autoscaler (HPA) with CPU targets of **70%** and **90%**.

---

## 1. System Overview

### 1.1 Topology

```
+----------------+     workload.txt      +----------------+
| Load tester   |  ----------------->  |  Dispatcher    |
| (client)      |     HTTP requests     |  (FIFO queue)  |
+--------+-------+                       +--------+-------+
         | client latency (histograms)              | 1 req/pod routing
         |                                           v
         |                                   +-----------------+
         |                                   |   K8s Service   |
         |                                   |  (ClusterIP)    |
         |                                   +--------+--------+
         |                                            |
         |                                            |
         |                                   +--------+--------+
         |                                   v                 v
         |                              Pod infer-1       Pod infer-N
         |                             (1 req at a time) (CPU limit 1)
         |
         v
+----------------+    PromQL     +-----------------+   patch replicas
|   Prometheus   | <----------   |   Autoscaler    |  -------------> API K8s
+----------------+               |    (MAPE 15s)  |
        ^                        +-----------------+
        | pod/dispatcher metrics
+------------------+
| HPA (baselines) |
|  scale on CPU   |
+------------------+
```

### 1.2 Design Principles

| Principle | Rationale |
|----------|-----------|
| **Single queue (dispatcher)** | The brief requires inference to process **one request at a time** per replica; all congestion must be observable at the dispatcher. |
| **No internal queue inside pods** | Otherwise the "queue depth" metric no longer represents the true system backlog. |
| **Decision every 15 s** | Aligns with HPA reconciliation cadence; ensures fair comparison. |
| **Max +/-1 replica per cycle** | Limits thrashing; HPA also effectively applies bounded rate of change. |
| **Slow scale-down (hysteresis)** | Example: 60-90 s of low load before reducing capacity; analogous to HPA `scaleDown stabilizationWindowSeconds` (~5 min in production, shortened for the coursework). |

### 1.3 Hardware Assumptions

- **Minikube** (or kind) on a laptop, **CPU only**.
- Each inference pod: `resources.requests/limits.cpu: "1"`, `memory: 1Gi`.
- `torch.set_num_threads(1)` in `model_server.py` (already present) to match CPU quota.

---

## 2. Recommended Repository Structure

```
cloud-computing/
├── model_server.py          # Inference service (existing, aiohttp)
├── client.py                # Local unit test
├── workload.txt             # QPS trace (provided or generated)
├── docker/
│   ├── Dockerfile.inference
│   ├── Dockerfile.dispatcher
│   └── Dockerfile.loadtester
├── k8s/
│   ├── namespace.yaml
│   ├── inference-deployment.yaml
│   ├── inference-service.yaml
│   ├── dispatcher-deployment.yaml
│   ├── prometheus/
│   │   ├── configmap.yaml
│   │   └── deployment.yaml
│   ├── metrics-server.yaml      # Required for HPA
│   ├── hpa-70.yaml
│   ├── hpa-90.yaml
│   └── autoscaler-deployment.yaml  # Optional: custom controller pod
├── src/
│   ├── dispatcher/
│   │   └── app.py               # Queue + routing + /metrics
│   ├── load_tester/
│   │   └── run.py               # Reads workload.txt, exports latencies
│   ├── exporters/               # If metrics are produced outside the main process
│   └── autoscaler/
│       ├── controller.py        # MAPE loop
│       ├── prometheus_client.py
│       ├── k8s_client.py
│       └── policies/
│           ├── queue_policy.py
│           └── pid_policy.py    # Advanced option
├── benchmarks/
│   ├── record_run.py            # Snapshot metrics -> CSV/JSON
│   └── plot_results.py          # p99 graphs + CPU/pods
├── tests/
│   ├── test_prometheus_queries.py
│   ├── test_scaling_logic.py
│   └── test_k8s_patch.py
└── docs/
    └── AUTOSCALER.md            # This file
```

---

## 3. Detailed Components

### 3.1 Inference Service

**Base:** `model_server.py` (aiohttp, ResNet18, endpoint `POST /infer`).

**Minimal K8s production extensions:**

| Endpoint | Role |
|----------|------|
| `GET /healthz` | Liveness: process is alive. |
| `GET /readyz` | Readiness: model is loaded. |
| `GET /metrics` | Prometheus: request counters and histogram `inference_duration_seconds`. |

**Prometheus instrumentation (Python client):**

```python
from prometheus_client import Counter, Histogram, generate_latest

REQUESTS = Counter("inference_requests_total", "Total inference requests")
LATENCY = Histogram(
    "inference_duration_seconds",
    "Server-side inference latency",
    buckets=[0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.0],
)

# Inside infer(): LATENCY.observe(duration); REQUESTS.inc()
```

**Important constraint:** the `infer` handler must remain **sequential** (one inference at a time per pod). Do not add an asyncio worker pool that would process multiple requests in parallel on the same pod.

### 3.2 Dispatcher (central queue)

**Responsibilities:**

1. Receive requests from the load tester (`POST /submit` or proxy to `/infer`).
2. Maintain a **bounded queue** (`asyncio.Queue(maxsize=Q_max)` or `queue.Queue`).
3. Assign each request to **an available replica** (round-robin or "first available pod").
4. Expose backlog and apply a **refusal policy** (HTTP 503) if the queue exceeds `Q_max`.

**Dispatcher metrics (mandatory for the autoscaler):**

| Metric | Type | Description |
|--------|------|-------------|
| `dispatcher_queue_depth` | Gauge | Number of requests waiting |
| `dispatcher_requests_in_flight` | Gauge | Requests currently processed by pods |
| `dispatcher_requests_total` | Counter | Arrivals |
| `dispatcher_requests_completed_total` | Counter | Successful completions |
| `dispatcher_requests_dropped_total` | Counter | Refusals (queue full) |

**Simple routing scheme:**

```
arrival -> enqueue -> worker loop:
  for each request:
    choose pod i with in_flight[i] == 0
    POST http://inference-service/infer
    decrement queue_depth
```

### 3.3 Load tester

**Input:** `workload.txt` - one line per interval (e.g., `timestamp,qps` or only `qps` every 15 s).

**Behavior:**

- Generates traffic based on the trace (Poisson or deterministic bursts depending on the file).
- Measures **end-to-end latency** (client -> dispatcher -> inference -> response).
- Exports to Prometheus (pushgateway **or** `/metrics` endpoint on the load tester):

| Metric | Type |
|--------|------|
| `loadtester_request_duration_seconds` | Histogram (buckets around 0.5 s) |
| `loadtester_requests_total` | Counter |

The **p50, p90, p99** percentiles are computed in Prometheus:

```promql
histogram_quantile(0.99, sum(rate(loadtester_request_duration_seconds_bucket[1m])) by (le))
```

For autoscaling, prioritize **server latency** (`inference_duration_seconds`) for decisions, and **client latency** for the report graphs (includes queue waiting time).

### 3.4 Prometheus and Metrics Server

**Prometheus** (`k8s/prometheus/`):

- Scrape interval: **15 s** (aligned with autoscaler / HPA cadence).
- Jobs: `inference`, `dispatcher`, `loadtester`, and optionally `kubernetes-pods` via annotations.

**Metrics Server:** required for `kubectl top` and for HPA runs (`resource.metrics.k8s.io`).

**Example pod annotations:**

```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "8001"
    prometheus.io/path: "/metrics"
```

---

## 4. MAPE Loop - Controller Implementation

### 4.1 Execution cycle (every 15 s)

```python
INTERVAL_SEC = 15
DEPLOYMENT = "inference"
NAMESPACE = "default"
REPLICA_MIN = 1
REPLICA_MAX = 10  # adapt to Minikube
MAX_DELTA_PER_CYCLE = 1
```

| Phase | Action |
|-------|--------|
| **Monitor** | PromQL queries: `queue_depth`, `p99_latency`, `cpu_util`, `ready_replicas`, `arrival_rate`. |
| **Analyze** | Compare p99 to `SLO = 0.5`, detect rising queue, compute capacity error. |
| **Plan** | Compute `desired_replicas` using the chosen policy (§4.2). |
| **Execute** | `PATCH` Deployment `spec.replicas` if `desired != current` (max +/-1). |

### 4.2 Recommended Policy: Queue + SLO Hybrid

This policy combines **reactive sizing based on queue** with a **latency guardrail (SLO)**.

**Parameters (calibrate on a reference trace):**

| Symbol | Initial value | Role |
|---------|----------------|------|
| `SLO` | 0.5 s | Target latency |
| `S_warn` | 0.45 s | Alert threshold (margin before violation) |
| `α` | 2-5 | Queue threshold for aggressive scale-up |
| `S̄` | ~0.15-0.25 s | Average service time per request (measured) |
| `headroom` | 1.2 | Margin over theoretical capacity |
| `drain_target` | 10 s | Time to drain surplus backlog |
| `cooldown_down` | 60-90 s | Stable duration before scale-down |

**Base formula (Little's Law capacity):**

The system can process at most `N / S̄` requests/s with `N` replicas (one request per pod at a time).

```
λ = arrival rate (req/s) over a 60 s sliding window
N_base = ceil(λ * S̄ * headroom)
N_queue = ceil(queue_depth * S̄ / drain_target)
N_desired = clamp(N_min, N_max, max(N_base, N_queue, N_current))
```

**Decision rules:**

1. **Fast scale-up** if `p99 > S_warn` **or** `queue_depth > α` for **two consecutive cycles**:
   - `N_desired = min(N_max, N_current + 1)` (priority over the formula when urgent).
2. **Moderate scale-up** if `N_desired > N_current` (from the formula):
   - `N_new = N_current + 1`.
3. **Scale-down** only if **all** conditions hold for `cooldown_down`:
   - `queue_depth == 0`
   - `p99 < 0.35 s` (below-SLO margin)
   - `N_desired < N_current`
   - then `N_new = N_current - 1`.
4. **Otherwise:** keep `N_current`.

**Why better than HPA?** HPA uses:

```
desiredReplicas = ceil(currentReplicas × currentCPU / targetCPU)
```

It does not see the queue or latency. During bursts, the queue grows **before** the average CPU over 15 s exceeds 70%, which can violate the SLO.

### 4.3 PID Variant (optional)

For smoother control:

```
e(t) = p99_measured - SLO
u(t) = Kp*e(t) + Ki*∫e(t)dt + Kd*de/dt
N_desired = round(N_current + u(t))
```

| Parameter | Initial value | Note |
|-----------|------------------|------|
| `Kp` | 2-5 | Response to latency deviation |
| `Ki` | 0.1-0.5 | Avoids persistent error |
| `Kd` | 0.5-1 | Dampens oscillations |

**Anti-windup:** freeze `Ki` when `N_current == N_max`. Always combine with the rule "max +/-1 replica per cycle".

### 4.4 Predictive Variant (bonus)

On Prometheus history (5-10 min window):

```
λ_pred = EWMA(λ, α=0.3)   # exponential smoothing
N_desired = ceil(λ_pred * S̄ * headroom)
```

Useful to anticipate peaks in `workload.txt`; enable only after stabilizing the reactive version.

### 4.5 Kubernetes Execution

**Option A - official Python client:**

```python
from kubernetes import client, config

config.load_in_cluster_config()  # or load_kube_config() locally
apps = client.AppsV1Api()
body = {"spec": {"replicas": desired}}
apps.patch_namespaced_deployment_scale(
    name=DEPLOYMENT,
    namespace=NAMESPACE,
    body=body,
)
```

**Option B - subprocess (quick prototype, less clean):**

```bash
kubectl scale deployment/inference --replicas=N
```

**Security:** use a dedicated ServiceAccount with a narrowly-scoped `Role` (only `patch`/`get` on the specific Deployment).

---

## 5. Reference PromQL Queries

```promql
# Queue depth
dispatcher_queue_depth

# Server p99 latency (1m window)
histogram_quantile(
  0.99,
  sum(rate(inference_duration_seconds_bucket[1m])) by (le)
)

# Client p99 latency (load tester)
histogram_quantile(
  0.99,
  sum(rate(loadtester_request_duration_seconds_bucket[1m])) by (le)
)

# Arrival rate
rate(dispatcher_requests_total[1m])

# Average CPU across inference pods (if cAdvisor/kubelet is available)
avg(rate(container_cpu_usage_seconds_total{pod=~"inference-.*"}[1m]))

# Ready replicas
kube_deployment_status_replicas_available{deployment="inference"}
# (requires kube-state-metrics or an equivalent source)
```

**Alternative without kube-state-metrics:** read `spec.replicas` from the Kubernetes API during the Monitor phase.

---

## 6. Comparison with HPA

### 6.1 HPA Configuration

**HPA 70%:**

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: inference-hpa-70
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: inference
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
```

Duplicate with `averageUtilization: 90` for the second baseline.

### 6.2 Experimental Protocol (fairness)

| Rule | Detail |
|-------|--------|
| Same `workload.txt` | Identical for custom autoscaler, HPA-70, HPA-90. |
| Same duration | Example: 30-45 min covering spikes and dips. |
| Warm-up | 2-3 min before recording metrics. |
| One active autoscaler | Disable HPA during the custom run (and vice versa). |
| Same `minReplicas` / `maxReplicas` | Comparison over the same envelope. |

### 6.3 Recorded metrics (every 15 s)

CSV file per run: `benchmarks/runs/<run_id>.csv`

| Column | Description |
|---------|-------------|
| `timestamp` | Unix or ISO |
| `replicas` | Number of inference pods |
| `p99_latency_s` | p99 (client or server; document which one) |
| `queue_depth` | Dispatcher backlog |
| `cpu_cores` | Sum of CPU across inference pods |
| `cpu_util_pct` | Average utilization |

### 6.4 Expected Graphs (matplotlib)

**Graph 1 - p99 vs time**

- Curve: custom autoscaler (primary color).
- Curves: HPA 70%, HPA 90%.
- Red horizontal line at **0.5 s** (SLO).
- Legend, labeled axes, explicit title.

**Graph 2 - Resources vs time**

- Y axis: **number of pods** or **consumed CPU cores** (sum).
- Same three curves + optionally a shaded SLO-respecting zone.

**Expected analysis in the report:**

- % of time where p99 < 0.5 s.
- Total **core-seconds** (integral of CPU over time): energy/cost efficiency.
- HPA lag during load increases (queue rises before CPU).
- HPA over-provisioning at 70% vs HPA under-performance at 90%.

---

## 7. Unit and Integration Tests

### 7.1 Tests for the scaling logic (without a cluster)

```python
# tests/test_scaling_logic.py
def test_scale_up_on_high_p99():
    assert plan_replicas(
        current=2, p99=0.55, queue=0, ...
    ) == 3

def test_no_scale_down_without_cooldown():
    ...

def test_max_delta_one():
    assert plan_replicas(current=2, desired_raw=5) == 3
```

### 7.2 Prometheus Tests (mock HTTP)

Mock JSON responses for `/api/v1/query` with synthetic metric vectors.

### 7.3 K8s Tests (kind / minikube CI optional)

- Deploy at 1 replica -> patch to 2 -> verify `kubectl get deploy`.
- Verify autoscaler ServiceAccount RBAC.

### 7.4 Isolated Component Tests

| Component | Test |
|-----------|------|
| Inference | `curl /infer` + `/metrics` |
| Dispatcher | Parallel sends, verify `queue_depth` |
| Load tester | Short excerpt of `workload.txt` |
| Autoscaler | Dry-run mode (`--dry-run` logs without patch) |

---

## 8. Technical Choices - Summary

| Decision | Selected choice | Rejected alternative | Rationale |
|----------|------------------|----------------------|-----------|
| Inference framework | aiohttp + PyTorch (existing) | TorchServe, KServe | Simplicity, full metric control, laptop-friendly |
| Queue | Dedicated dispatcher | Queue inside each pod | Observability, matches the brief |
| Metrics | Prometheus pull | Logs only | PromQL, percentiles, HPA comparison |
| Controller | Python + Kubernetes client | `kubectl` shell | Testable, idempotent, fine-grained RBAC |
| Policy | Queue + SLO + hysteresis | CPU only (HPA) | Latency SLO, leading indicator |
| Interval | 15 s | 5 s / 60 s | HPA parity, Minikube stability |
| Variation | +/-1 / cycle | Direct scaling to `N_desired` | Anti-thrashing |

---

## 9. Threshold Calibration (Method)

1. **Measure `S̄`:** deploy 1 replica, run low constant load, read `histogram_quantile(0.5, inference_duration_seconds)`.
2. **Find `α`:** run load until p99 is around 0.45 s with 1 replica; note average `queue_depth`.
3. **Validate `headroom`:** run full trace; if p99 often exceeds 0.5 s, increase `headroom` or lower `S_warn`.
4. **Adjust `cooldown_down`:** if replica oscillations occur, increase (90 -> 120 s).

Log every run (`run_id`, git commit, parameters) in `benchmarks/runs/metadata.json`.

---

## 10. Quick Deployment Checklist

- [ ] Minikube running, `metrics-server` OK (`kubectl top nodes`)
- [ ] Docker images built and loaded into Minikube
- [ ] Prometheus scrapes all targets (`/targets` UI)
- [ ] Inference with 1 replica responds via dispatcher
- [ ] Load tester exports points to `loadtester_request_duration_seconds`
- [ ] Autoscaler in dry-run mode: logs consistent for 5 min
- [ ] Custom run + export CSV
- [ ] Disable autoscaler, enable HPA-70, run + export
- [ ] Enable HPA-90, run + export
- [ ] `plot_results.py` -> 2 PNG figures for the report

---

## 11. Known Limits and Extensions

| Limit | Mitigation |
|--------|------------|
| Cold start of new pods (model loading) | `minReadySeconds`, strict readiness probe |
| PromQL `rate()` on short windows | Window >= 1m, longer warm-up |
| Shared Minikube CPU | Repeat runs; use the median of 3 trials |
| Pushgateway vs pull | Prefer HTTP scrape on the load tester |

**Extensions (if time):** full PID, EWMA prediction, M/M/c queueing model for optimal `N`, comparison with VPA or KEDA (out of minimal scope).

---

## 12. Project Success Criteria

1. **SLO:** p99 < 0.5 s for a significant fraction of the custom run (> HPA on the same trace, or same SLO with fewer core-seconds).
2. **Stability:** no wild oscillations in replicas (visualize on Graph 2).
3. **Reproducibility:** scripts + YAML + versioned parameters.
4. **Clarity:** presentation explaining the MAPE loop, metrics, and why CPU-only is insufficient.

---

## References

- MAPE loop: Monitor - Analyze - Plan - Execute (cloud computing course).
- [Kubernetes HPA](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/) - formula `desiredReplicas = ceil(current × metric / target)`.
- [Prometheus histogram_quantile](https://prometheus.io/docs/prometheus/latest/querying/functions/#histogram_quantile).
- Local base code: `model_server.py`, `practical_HandsOn (1).md`.

