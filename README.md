# Elastic ML Inference Serving

Kubernetes-based **elastic ML serving** project: ResNet18 image classification (CPU-only), centralized queue, Prometheus monitoring, load tester, and a custom autoscaler driven by latency and queue depth (15 s MAPE loop).

**Primary SLO:** server-side p99 latency **< 0.5 s**, measured as `dispatcher_request_duration_seconds` (queue wait + inference).

> 📄 **Authoritative design & results:** [`experiments/results/REPORT.md`](experiments/results/REPORT.md) is the complete write-up (architecture, autoscaler design + parameter rationale, the custom-vs-HPA comparison, and the hardware-limit analysis). The other docs are component references; where they disagree, REPORT.md is current.

---

## Architecture

```mermaid
flowchart LR
  LT[LoadTester] -->|POST /submit| D[Dispatcher]
  D -->|POST /infer| INF[Inference pods]
  LT -->|GET /metrics :8003| PROM[Prometheus]
  D -->|GET /metrics :8002| PROM
  INF -->|GET /metrics :8001| PROM
  PROM --> AS[Custom Autoscaler]
  AS -->|patch replicas| K8S[K8s API]
  HPA[HPA baselines 70/90% CPU] --> K8S
```

| Component | Port | Role |
|-----------|------|------|
| Inference (`model_server.py`) | 8001 | ResNet18, CPU, threads pinned to 1, weights baked in, 1 request per pod at a time |
| Dispatcher (`src/dispatcher/app.py`) | 8002 | Bounded queue (3) + **headless per-pod dispatch** (one in-flight per pod), 503 shedding |
| Load tester (`src/load_tester/run.py`) | 8003 (metrics) | Triangle load profile + CSV export |
| Autoscaler (`src/autoscaler/controller.py`) | — | MAPE loop every 15 s on queue + arrival + p99 (min/max = 1/3) |
| Prometheus | 9090 | 15 s scrape interval |

---

## Quick start (local)

### 1. Environment

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/macOS
source venv/bin/activate

pip install torch==2.3.0 torchvision==0.18.0 --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
pip install pillow opencv-python
```

### 2. Run the stack (3 terminals)

**Terminal 1 — Inference:**
```bash
python model_server.py
```

**Terminal 2 — Dispatcher:**
```bash
# Windows
set INFERENCE_URL=http://127.0.0.1:8001
# Linux/macOS
export INFERENCE_URL=http://127.0.0.1:8001
python src/dispatcher/app.py
```

**Terminal 3 — Load tester:**
```bash
python src/load_tester/run.py --target http://127.0.0.1:8002 --duration 60 --base 2 --peak 10
```

### 3. Check metrics

```bash
curl http://127.0.0.1:8001/metrics | findstr inference_duration
curl http://127.0.0.1:8002/metrics | findstr dispatcher_queue
curl http://127.0.0.1:8003/metrics | findstr loadtester_request
```

### 4. Single-request smoke test

```bash
python client.py
```

---

## Kubernetes deployment

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/inference-deployment.yaml
kubectl apply -f k8s/dispatcher-deployment.yaml
kubectl apply -f k8s/prometheus/
kubectl apply -f k8s/autoscaler-deployment.yaml
kubectl apply -f k8s/loadtester-job.yaml   # one-shot benchmark
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for details (Docker images, Minikube, autoscaler dry-run).

---

## Repository structure

```
cloud-computing/
├── model_server.py              # ResNet18 inference service
├── client.py                    # Local smoke-test client
├── requirements.txt             # Python deps (excluding torch)
├── docker/                      # one Dockerfile per service
│   ├── Dockerfile.inference
│   ├── Dockerfile.dispatcher
│   ├── Dockerfile.autoscaler
│   └── Dockerfile.loadtester
├── k8s/
│   ├── namespace.yaml
│   ├── inference-deployment.yaml
│   ├── dispatcher-deployment.yaml
│   ├── loadtester-job.yaml
│   ├── autoscaler-deployment.yaml
│   ├── hpa-70.yaml              # HPA baseline (70% CPU)
│   ├── hpa-90.yaml              # HPA baseline (90% CPU)
│   └── prometheus/
├── src/
│   ├── dispatcher/app.py        # Queue + workers + forwarding
│   ├── load_tester/
│   │   ├── run.py               # Load generator (triangle + --workload replay)
│   │   ├── images.py            # Samples as base64 (synthetic fallback)
│   │   └── workload.txt         # Bursty per-second RPS trace
│   └── autoscaler/              # MAPE controller + Queue+SLO policy
├── experiments/                 # Benchmark harness (collect.py, plot.py)
├── reference/                   # Course materials (PDF, hands-on, notes)
├── tests/
└── docs/
    ├── ARCHITECTURE.md
    ├── AUTOSCALER.md
    ├── DISPATCHER.md
    ├── LOAD_TESTER.md
    └── DEPLOYMENT.md
```

---

## Tests

```bash
python -m pytest tests/ -v
```

| File | Coverage |
|------|----------|
| `test_scaling_logic.py` | Queue+SLO policy |
| `test_prometheus_queries.py` | PromQL client |
| `test_k8s_patch.py` | K8s replica patch |
| `test_dispatcher_forward.py` | Dispatcher E2E forwarding |
| `test_load_tester.py` | RPS profile, payload, metrics |

---

## Documentation

| Document | Content |
|----------|---------|
| [experiments/results/REPORT.md](experiments/results/REPORT.md) | **Authoritative**: full design, parameter rationale, results, hardware-limit analysis |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System overview, data flow, metrics |
| [docs/DISPATCHER.md](docs/DISPATCHER.md) | API, queue, workers, env vars |
| [docs/LOAD_TESTER.md](docs/LOAD_TESTER.md) | Triangle profile, CLI, Prometheus, K8s Job |
| [docs/AUTOSCALER.md](docs/AUTOSCALER.md) | Custom autoscaler (MAPE, policy, PromQL) |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Minikube deployment, the 3-run experiment, troubleshooting |
| [experiments/README.md](experiments/README.md) | Benchmark harness (collect + plot) |
| [reference/practical_HandsOn.md](reference/practical_HandsOn.md) | Initial inference-only tutorial (course material) |

---

## Implementation status

| Component | Status |
|-----------|--------|
| Inference + `/metrics` | Implemented |
| Dispatcher synchronous forwarding | Implemented |
| Load tester (merged from `load-tester` branch) | Implemented |
| Prometheus scrape (inference, dispatcher, loadtester) | Implemented |
| Autoscaler MAPE + Queue+SLO policy | Implemented (real scaling by default; `--dry-run` optional) |
| Container images (inference / dispatcher / autoscaler / loadtester) | Implemented (`docker/Dockerfile.*`) |
| HPA 70% / 90% baselines | Implemented (`k8s/hpa-70.yaml`, `k8s/hpa-90.yaml`) |
| `workload.txt` bursty trace | Implemented (`--workload` replay; trace shipped in the loadtester image) |
| Benchmark plots / harness | Implemented (`experiments/collect.py`, `experiments/plot.py`) |

---

## Git branches

| Branch | Content |
|--------|---------|
| `main` | Project base |
| `infra-setup` | Initial K8s manifests |
| `elastic-autoscaler` | Autoscaler + dispatcher + integrated load tester |
| `load-tester` | Sakshi's script (merged into `elastic-autoscaler`) |
