# Kubernetes deployment

End-to-end guide to deploy the elastic ML inference stack on **Minikube** and run
the comparison experiment (custom autoscaler vs HPA 70% / 90%).

> **Shell note.** Commands are written for `bash`. On **Windows**, run them from
> Git Bash, or translate to PowerShell where noted (most `kubectl` / `docker` /
> `minikube` commands are identical). PowerShell-specific differences are called
> out in [§9](#9-windows--powershell-notes).

---

## TL;DR (scripts)

On Windows PowerShell, the whole flow is wrapped in three scripts:

```powershell
pwsh ./scripts/install.ps1      # start Minikube, build images, deploy the stack
pwsh ./scripts/smoke_test.ps1   # verify the chain end-to-end (PASS/FAIL)
pwsh ./scripts/run_all.ps1      # custom autoscaler vs HPA 70/90, plots the figures
```

The sections below explain each step manually (and for bash users).

---

## 0. Prerequisites

- Docker (running) — used to build the service images.
- `minikube` and `kubectl`.
- `metrics-server` enabled in the cluster (required for the HPA — see step 1).

---

## 1. Start the cluster

```bash
minikube start --cpus=4 --memory=6g --driver=docker
minikube addons enable metrics-server   # REQUIRED for the HPA (else it shows <unknown>)
kubectl get nodes                        # should be Ready
```

---

## 2. Build the images

The cluster uses local images with `imagePullPolicy: IfNotPresent`.

> **Recommended: build inside Minikube's Docker daemon (`docker-env`).**
> On Docker Desktop / Windows, modern `docker build` (buildx) emits an OCI
> manifest-list with provenance attestations, and `minikube image load` does
> **not** load it reliably — the cluster can keep running a stale image. Building
> directly into Minikube's daemon avoids the load step entirely and is the most
> reliable method.

Point Docker at Minikube's daemon, disable BuildKit (so the legacy builder
produces a plain single-arch image), then build from the repository root:

```bash
# bash (Git Bash):  eval $(minikube docker-env)
# PowerShell:       & minikube docker-env --shell powershell | Invoke-Expression
eval $(minikube docker-env)
export DOCKER_BUILDKIT=0          # PowerShell: $env:DOCKER_BUILDKIT=0

docker build -t inference:latest  -f docker/Dockerfile.inference  .
docker build -t dispatcher:latest -f docker/Dockerfile.dispatcher .
docker build -t autoscaler:latest -f docker/Dockerfile.autoscaler .
docker build -t loadtester:latest -f docker/Dockerfile.loadtester .
```

No `minikube image load` is needed — the kubelet uses this same daemon.

> The `inference` image pulls CPU-only torch/torchvision (~200 MB); the first
> build takes several minutes. The `workload.txt` trace is baked into the
> `loadtester` image at `/app/load_tester/workload.txt`.

Verify the images contain their deps (catches a broken build before deploying):
```bash
minikube image ls | grep -E "inference|dispatcher|autoscaler|loadtester"
docker run --rm inference:latest pip show aiohttp   # must print aiohttp 3.9.5
```

> **Fallback (`docker build` + load):** if you build against the host daemon
> instead, you must `minikube image load <name>:latest` after every build — and
> verify it actually updated the in-cluster image. If a pod still runs old code
> after a reload, delete the stale image first:
> `minikube image rm inference:latest` then reload, or just use `docker-env`.

---

## 3. Apply manifests in order (wait for Ready at each step)

```bash
kubectl apply -f k8s/namespace.yaml

kubectl apply -f k8s/inference-deployment.yaml
kubectl -n inference-system rollout status deploy/inference     # MUST become Ready

kubectl apply -f k8s/dispatcher-deployment.yaml
kubectl -n inference-system rollout status deploy/dispatcher

kubectl apply -f k8s/prometheus/
kubectl -n inference-system rollout status deploy/prometheus
```

Check everything is up:
```bash
kubectl -n inference-system get pods    # all Running, READY 1/1
```

> If `inference` stays `0/1`, inspect the readiness probe (`/readyz`):
> `kubectl -n inference-system describe pod <pod>` and `... logs <pod>`.

---

## 4. Services and ports

| Service | Port | Endpoints |
|---------|------|-----------|
| `inference` | 8001 | `POST /infer`, `/metrics`, `/healthz`, `/readyz` |
| `dispatcher` | 8002 | `POST /submit`, `/metrics`, `/healthz` |
| `prometheus` | 9090 | UI + PromQL API |
| `loadtester` | 8003 | `/metrics` (while the Job runs) |

The image contract is base64: `POST /submit` and `/infer` both take
`{"data": "<base64-jpeg>"}`.

---

## 5. Verify the metric pipeline (key check)

```bash
kubectl -n inference-system port-forward svc/prometheus 9090:9090
```

Open `http://localhost:9090/targets` — `inference`, `dispatcher`, `loadtester`
should be **UP**. In the *Graph* tab run the p99 query:

```promql
histogram_quantile(0.99, sum(rate(inference_duration_seconds_bucket[1m])) by (le))
```

It must return a numeric value (not "no data") once traffic is flowing. This
proves the inference `/metrics` endpoint is scraped correctly and that the
autoscaler can see latency.

---

## 6. Custom autoscaler

Manifest: `k8s/autoscaler-deployment.yaml` (ServiceAccount + RBAC + Deployment).
It performs **real scaling** by default (`args: []`): it computes scaling
decisions and patches the inference Deployment. To observe without patching,
override with `args: ["--dry-run"]` and re-apply.

```bash
kubectl apply -f k8s/autoscaler-deployment.yaml
kubectl -n inference-system logs -f deploy/custom-autoscaler
```

Expected log lines under load:
```
MAPE decision reason=... queue_depth=... p99=... current=... desired=...
```

Key env vars (already set in the manifest):
- `DEPLOYMENT_NAMESPACE=inference-system`
- `PROMETHEUS_URL=http://prometheus:9090`
- `INTERVAL_SEC=15`

The RBAC Role grants `patch` on `deployments/scale`, which is what the controller
uses to apply the scaling decisions.

---

## 7. The comparison experiment (3 runs)

Goal: replay the same `workload.txt` trace three times — once with the custom
autoscaler, once with HPA at 70% CPU, once at 90% — and compare p99 latency and
CPU cores used.

**Rules for a valid comparison:**
- Only **one** scaler active at a time (never HPA + custom on the same Deployment).
- Same workload, same pod resources (CPU request/limit = 1, already set).
- Reset to `minReplicas` between runs and let it settle.

Install the harness tooling once and keep Prometheus port-forwarded
(see [experiments/README.md](../experiments/README.md)):
```bash
pip install -r experiments/requirements.txt
kubectl -n inference-system port-forward svc/prometheus 9090:9090 &
```

`collect.py` samples `timestamp, p99_latency, replica_count, cpu_cores` every 15 s
(p99 from Prometheus; replicas and CPU from the Kubernetes API / metrics-server,
because the Prometheus config scrapes the Service DNS and cannot count replicas).

**Run 1 — custom autoscaler** (real scaling is the default):
```bash
kubectl -n inference-system delete hpa --all
kubectl apply -f k8s/autoscaler-deployment.yaml
python experiments/collect.py --out custom.csv &
kubectl apply -f k8s/loadtester-job.yaml
kubectl -n inference-system wait --for=condition=complete job/loadtester --timeout=900s
# stop collect.py (Ctrl-C / kill)
```

**Run 2 — HPA 70%:**
```bash
kubectl -n inference-system delete deploy custom-autoscaler
kubectl -n inference-system delete job loadtester
kubectl -n inference-system scale deploy/inference --replicas=1
kubectl apply -f k8s/hpa-70.yaml
python experiments/collect.py --out hpa70.csv &
kubectl apply -f k8s/loadtester-job.yaml
kubectl -n inference-system wait --for=condition=complete job/loadtester --timeout=900s
```

**Run 3 — HPA 90%:**
```bash
kubectl -n inference-system delete hpa inference-hpa
kubectl -n inference-system delete job loadtester
kubectl -n inference-system scale deploy/inference --replicas=1
kubectl apply -f k8s/hpa-90.yaml
python experiments/collect.py --out hpa90.csv &
kubectl apply -f k8s/loadtester-job.yaml
kubectl -n inference-system wait --for=condition=complete job/loadtester --timeout=900s
```

**Produce the figures:**
```bash
python experiments/plot.py custom.csv hpa70.csv hpa90.csv --out-prefix comparison
# -> comparison_p99.png, comparison_cpu.png
```

---

## 8. Deployment checklist

- [ ] Minikube running, `metrics-server` enabled
- [ ] All 4 images built and loaded into Minikube
- [ ] All pods `Running` / `READY 1/1` in `inference-system`
- [ ] Prometheus targets **UP** (inference, dispatcher)
- [ ] p99 PromQL returns a number under load
- [ ] Autoscaler logs `MAPE decision` every 15 s (and patches replicas)
- [ ] Load tester Job completes without mass errors
- [ ] 3 runs collected (`custom.csv`, `hpa70.csv`, `hpa90.csv`) and plotted

---

## 9. Windows / PowerShell notes

- Use Git Bash to run the bash blocks verbatim, or translate to PowerShell.
- Backgrounding (`&`) is bash-only. In PowerShell, run `collect.py` and the
  port-forward in separate terminals instead.
- Build images via Minikube's Docker daemon (see [§2](#2-build-the-images)):
  ```powershell
  & minikube docker-env --shell powershell | Invoke-Expression
  $env:DOCKER_BUILDKIT=0
  docker build -t inference:latest -f docker/Dockerfile.inference .
  # ...repeat for dispatcher / autoscaler / loadtester (no minikube image load)
  ```
- If `minikube` is not on PATH, call it by full path, e.g.
  `& "C:\Program Files\Kubernetes\Minikube\minikube.exe" ...`.
- Install dependencies with `python -m pip install -r experiments/requirements.txt`.

---

## 10. Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `ErrImagePull` / `ImagePullBackOff` | Image not in cluster | Build via `docker-env` (§2), or `minikube image load <name>:latest` |
| Pod crashes with old code after a rebuild | `minikube image load` loaded a stale buildx image | Use `docker-env` build (§2); verify with `docker run --rm <img> pip show <dep>` |
| `ModuleNotFoundError` in a pod | Missing dep in the image | Add it to the matching `docker/Dockerfile.*` and rebuild |
| inference pod `0/1` | `/readyz` probe failing | `kubectl describe pod` / check logs |
| `/metrics` returns 500 | charset in content-type (fixed) | Ensure services are rebuilt from current code |
| Prometheus p99 = "no data" | No traffic yet | Run the load tester first |
| HPA shows `<unknown>` | metrics-server off or no CPU requests | Enable metrics-server; requests are already set |
| `503 Queue is full` | Load > capacity | Scale replicas or raise `DISPATCHER_QUEUE_MAX_SIZE` |
| Replica patch fails | RBAC | Check ServiceAccount `autoscaler-sa` and the Role on `deployments/scale` |
| loadtester target DOWN | Job finished | Expected after the Job ends; re-apply the Job to scrape again |

---

## 11. Local validation before Kubernetes

Before deploying, validate the full chain locally with three processes — see
[README.md](../README.md) "Quick start" and [experiments/README.md](../experiments/README.md).
This catches contract/metric issues without cluster overhead.

---

## 12. Cleanup

```bash
kubectl delete namespace inference-system   # remove all workloads
minikube stop                               # or: minikube delete (full reset)
```
