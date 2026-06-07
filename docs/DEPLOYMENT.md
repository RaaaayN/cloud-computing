# Kubernetes deployment

Guide to deploy the full stack on **Minikube** (or a local cluster).

---

## Prerequisites

- Minikube / kind + `kubectl`
- Docker (image builds)
- `metrics-server` (required for HPA — see §8)

---

## 1. Namespace

```bash
kubectl apply -f k8s/namespace.yaml
```

Creates the `inference-system` namespace.

---

## 2. Docker images

### Load tester

```bash
docker build -f docker/Dockerfile.loadtester -t loadtester:latest .
minikube image load loadtester:latest
```

### Inference, dispatcher, autoscaler

*(Dedicated Dockerfiles for inference/dispatcher/autoscaler may be added — build manually or use local images for now.)*

Load a local image into Minikube:
```bash
minikube image load inference:latest
minikube image load dispatcher:latest
minikube image load autoscaler:latest
```

---

## 3. Manifest apply order

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/inference-deployment.yaml
kubectl apply -f k8s/dispatcher-deployment.yaml
kubectl apply -f k8s/prometheus/
kubectl apply -f k8s/autoscaler-deployment.yaml
```

Verify:
```bash
kubectl get pods -n inference-system
kubectl get svc -n inference-system
```

---

## 4. Services and ports

| Service | Port | Endpoints |
|---------|------|-----------|
| `inference` | 8001 | `POST /infer`, `/metrics` |
| `dispatcher` | 8002 | `POST /submit`, `/metrics` |
| `prometheus` | 9090 | UI + PromQL API |
| `loadtester` | 8003 | `/metrics` (during Job) |

**Access Prometheus (Minikube):**
```bash
kubectl port-forward -n inference-system svc/prometheus 9090:9090
```
Open http://localhost:9090/targets — verify jobs `inference`, `dispatcher`, `loadtester`.

---

## 5. Load tester (benchmark)

```bash
kubectl apply -f k8s/loadtester-job.yaml
kubectl logs -n inference-system job/loadtester -f
```

The Job:
- Sends traffic for 300 s to the dispatcher
- Triangle profile: base 1 req/s, peak 20 req/s
- Exposes `/metrics` on 8003 for Prometheus

**Re-run a benchmark:**
```bash
kubectl delete job loadtester -n inference-system
kubectl apply -f k8s/loadtester-job.yaml
```

---

## 6. Custom autoscaler

Manifest: `k8s/autoscaler-deployment.yaml`

**Default: dry-run mode** (`args: ["--dry-run"]`).

To enable real scaling, edit the Deployment:
```yaml
args: []   # remove --dry-run
```

Important env vars (already in the manifest):
- `DEPLOYMENT_NAMESPACE=inference-system`
- `PROMETHEUS_URL=http://prometheus:9090`
- `INTERVAL_SEC=15`

Logs:
```bash
kubectl logs -n inference-system deploy/custom-autoscaler -f
```

---

## 7. Prometheus

Files:
- [`k8s/prometheus/configmap.yaml`](../k8s/prometheus/configmap.yaml) — scrape configs
- [`k8s/prometheus/deployment.yaml`](../k8s/prometheus/deployment.yaml)

Scrape jobs (15 s interval):
```yaml
- inference:8001
- dispatcher:8002
- loadtester:8003
```

---

## 8. HPA baselines (planned)

Planned manifests: `k8s/hpa-70.yaml`, `k8s/hpa-90.yaml`.

**Important:** disable the custom autoscaler or HPA before each comparative run (only one controller active).

Prerequisites:
```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl top nodes
```

---

## 9. Deployment checklist

- [ ] Minikube running
- [ ] Images loaded into Minikube
- [ ] All pods `Running` in `inference-system`
- [ ] Prometheus targets **UP** (inference, dispatcher)
- [ ] Manual test: port-forward dispatcher → `POST /submit`
- [ ] Load tester Job completes without mass errors
- [ ] Autoscaler dry-run: `MAPE decision` logs every 15 s
- [ ] (Optional) Autoscaler active + load tester → replicas increase

---

## 10. Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `503 Queue is full` | Load > capacity | Scale replicas or increase `DISPATCHER_QUEUE_MAX_SIZE` |
| loadtester target DOWN | Job finished | Expected after Job ends; re-apply Job to scrape again |
| Autoscaler p99 = 0 | No traffic | Run load tester |
| Replica patch fails | RBAC | Check ServiceAccount `autoscaler-sa` |
| Inference OOM | Heavy model | Verify 1Gi memory limits |

---

## 11. Local validation before K8s

See [README.md](../README.md) § Quick start — validate inference + dispatcher + load tester locally before cluster deployment.
