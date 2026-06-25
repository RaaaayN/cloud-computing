# Cloud Computing Project 2026 — Windows Guide

> **Use Git Bash** (from Git for Windows) for all commands below. PowerShell/CMD syntax differs for port cleanup and background jobs.

General architecture and autoscaling logic are described in [README.md](README.md). This document covers **Windows-specific setup, ports, and troubleshooting** only.

---

## Prerequisites

| Tool | Notes |
|---|---|
| [Minikube](https://minikube.sigs.k8s.io/docs/start/) | `minikube start --cpus=4 --memory=6144` |
| Docker Desktop | Required driver for Minikube on Windows |
| kubectl | Usually bundled with Docker Desktop or Minikube |
| Python 3 | `pip install pandas matplotlib requests` |
| Git Bash | For `eval`, `&` background jobs, and Unix-style scripts |

---

## Port Map (read this first)

| Local port | Service | Notes |
|---|---|---|
| 5001 | Dispatcher API (`/query`) | Same as Linux |
| 8000 | Dispatcher metrics | Same as Linux |
| 8001 | Inference metrics | Same as Linux |
| **19090** | Prometheus UI / API | **Windows only — do not use 9090** |

### Why not port 9090?

Windows reserves TCP port **9090** inside the system excluded range **9081–9180** (Hyper-V / WinNAT). Attempting:

```bash
kubectl port-forward service/prometheus-service 9090:9090
```

fails with:

```text
bind: An attempt was made to access a socket in a way forbidden by its access permissions
```

**Fix:** forward Prometheus to **19090** locally:

```bash
kubectl port-forward service/prometheus-service 19090:9090 &
```

`autoscaler_logger.py` auto-detects Windows (`sys.platform == "win32"`) and queries `http://localhost:19090`. Override if needed:

```bash
export PROMETHEUS_URL=http://localhost:19090
```

Check reserved ranges on your machine:

```powershell
netsh interface ipv4 show excludedportrange protocol=tcp
```

If **19090** is also reserved, pick another free port (e.g. `29090`) in both `kubectl port-forward` and `PROMETHEUS_URL`.

---

## Step 1 — Start Minikube

```bash
minikube start --cpus=4 --memory=6144
minikube addons enable metrics-server
kubectl get nodes
```

Expected: `minikube   Ready   control-plane`

---

## Step 2 — Build Docker Images Inside Minikube

```bash
eval $(minikube docker-env)

cd ml_model && docker build -t resnet-infer . && cd ..
cd dispatcher && docker build -t dispatcher . && cd ..

docker images | grep -E "resnet-infer|dispatcher"
```

> Must run `eval $(minikube docker-env)` in **every new Git Bash session** before building, or pods will hit `ImagePullBackOff`.

---

## Step 3 — Deploy to Kubernetes

```bash
kubectl apply -f dispatcher/k8/redis-deployment.yaml
kubectl apply -f dispatcher/k8/inference-deployment.yaml
kubectl apply -f dispatcher/k8/inference-service.yaml
kubectl apply -f dispatcher/k8/prometheus-configmap.yaml
kubectl apply -f dispatcher/k8/prometheus-deployment.yaml
kubectl apply -f dispatcher/k8/dispatcher-deployment.yaml

kubectl get pods -w
```

Wait until all four pods show `1/1 Running`, then Ctrl+C.

After code changes, restart deployments to pick up new images:

```bash
kubectl rollout restart deployment/tu-cloud-project deployment/dispatcher
kubectl rollout status deployment/tu-cloud-project --timeout=180s
```

---

## Step 4 — Start Port Forwards (Windows)

Run in a **dedicated Git Bash window** and keep it open for the whole session.

### Kill stale listeners

```bash
for port in 5001 8000 8001 19090; do
  netstat -ano | grep ":$port " | awk '{print $5}' | sort -u | while read pid; do
    [ -n "$pid" ] && [ "$pid" != "0" ] && taskkill //F //PID "$pid" 2>/dev/null
  done
done
```

### Start forwards

```bash
kubectl port-forward service/dispatcher-service 5001:5001 &
kubectl port-forward service/dispatcher-service 8000:8000 &
kubectl port-forward service/tu-cloud-project 8001:8001 &
kubectl port-forward service/prometheus-service 19090:9090 &
sleep 2
```

### Verify

```bash
curl -s http://localhost:5001/query -X POST \
  -H "Content-Type: application/json" \
  -d '{"image": "/app/images/fire_truck.jpeg"}'
# Expected: {"message":"Queued"}

curl -s http://localhost:19090/api/v1/targets | python -m json.tool | grep health
# Both targets should show "health": "up"
```

Open Prometheus UI: http://localhost:19090

---

## Step 5 — Run Experiments

Each scenario runs a **630-second** load test (~10.5 min). All three scenarios take **~35–40 minutes** total.

### Option A — One-click script (recommended)

From repo root in Git Bash:

```bash
export PYTHONIOENCODING=utf-8
bash scripts/run_all.sh
```

Logs are written to `scripts/run_all.log`. The script runs Custom → HPA 70% → HPA 90%, then generates comparison plots.

### Option B — Manual (three terminals)

**Before each scenario:**

```bash
kubectl exec deployment/redis -- redis-cli flushall
kubectl scale deployment tu-cloud-project --replicas=1
echo "Timestamp,P99_Latency,Queue_Size,Replica_Count" > dispatcher/autoscaler_log.csv
```

#### Scenario A — Custom Autoscaler

Terminal 1:

```bash
cd dispatcher
export PYTHONIOENCODING=utf-8
python autoscaler_logger.py
```

Wait until queue reads `0`, then Terminal 2:

```bash
cd dispatcher/test
export PYTHONIOENCODING=utf-8
python test.py
```

When load test reaches **Second 630**, Ctrl+C Terminal 1, then:

```bash
cp dispatcher/autoscaler_log.csv dispatcher/custom_autoscaler_log.csv
```

#### Scenario B — HPA @ 70% CPU

```bash
kubectl delete hpa tu-cloud-project 2>/dev/null; true
kubectl autoscale deployment tu-cloud-project --cpu-percent=70 --min=1 --max=10
```

Terminal 1 (log only — do **not** let custom autoscaler fight HPA):

```bash
cd dispatcher
export PYTHONIOENCODING=utf-8
AUTOSCALER_LOG_ONLY=1 python autoscaler_logger.py
```

Terminal 2: `cd dispatcher/test && python test.py`

When done:

```bash
cp dispatcher/autoscaler_log.csv dispatcher/hpa70_log.csv
kubectl delete hpa tu-cloud-project
```

#### Scenario C — HPA @ 90% CPU

Same as Scenario B, but `--cpu-percent=90` and save to `hpa90_log.csv`.

---

## Step 6 — Generate Plots

```bash
cd dispatcher
python compare_autoscalers.py custom_autoscaler_log.csv hpa70_log.csv hpa90_log.csv
python analyze_autoscaler_log.py
```

Open plots (Windows):

```bash
start comparison_plot.png
start p99_latency_plot.png
start queue_size_plot.png
start replica_count_plot.png
```

---

## Windows Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `bind: ... access permissions` on 9090 | Windows reserved port range | Use **19090** (see above) |
| Autoscaler shows `N/A` for all metrics | Prometheus not reachable on expected port | Confirm `curl http://localhost:19090/-/ready` returns 200 |
| `UnicodeEncodeError: 'gbk'` in test.py | Console encoding | `export PYTHONIOENCODING=utf-8` before running Python |
| `ImagePullBackOff` | Images built outside Minikube Docker | Re-run `eval $(minikube docker-env)` and rebuild |
| Port-forward dies mid-test | Git Bash session closed | Restart Step 4 forwards, wait 30s for metrics |
| First CSV rows show `nan` / `N/A` | Prometheus warming up | Normal — wait 30s after port-forward before load test |
| `watch` not found | Not available on Windows | Use `while true; do kubectl get pods; sleep 5; done` |

**Kill a stuck port-forward:**

```bash
netstat -ano | grep ":5001 "
taskkill //F //PID <PID>
```

**Check HPA status:**

```bash
kubectl get hpa
kubectl top pods -l app=resnet-infer
```

---

## Environment Variables Reference

| Variable | Default (Windows) | Purpose |
|---|---|---|
| `PROMETHEUS_URL` | `http://localhost:19090` | Prometheus query endpoint |
| `AUTOSCALER_LOG_ONLY` | unset (scaling enabled) | Set to `1` during HPA scenarios |
| `PYTHONIOENCODING` | (system) | Set to `utf-8` to avoid GBK print errors |

---

## Expected Output Files

After a full run, `dispatcher/` should contain:

| File | Description |
|---|---|
| `custom_autoscaler_log.csv` | Custom autoscaler metrics log |
| `hpa70_log.csv` | HPA 70% run log |
| `hpa90_log.csv` | HPA 90% run log |
| `comparison_plot.png` | Three-way P99 / replica comparison |
| `p99_latency_plot.png` | P99 over time (last scenario) |
| `queue_size_plot.png` | Queue depth over time |
| `replica_count_plot.png` | Replica count over time |

---

## Quick Reference — Full Windows Workflow

```bash
# 1. Cluster + images
minikube start --cpus=4 --memory=6144
eval $(minikube docker-env)
cd ml_model && docker build -t resnet-infer . && cd ..
cd dispatcher && docker build -t dispatcher . && cd ..
kubectl apply -f dispatcher/k8/redis-deployment.yaml
kubectl apply -f dispatcher/k8/inference-deployment.yaml
kubectl apply -f dispatcher/k8/inference-service.yaml
kubectl apply -f dispatcher/k8/prometheus-configmap.yaml
kubectl apply -f dispatcher/k8/prometheus-deployment.yaml
kubectl apply -f dispatcher/k8/dispatcher-deployment.yaml

# 2. Port forwards (19090, not 9090!)
kubectl port-forward service/dispatcher-service 5001:5001 &
kubectl port-forward service/dispatcher-service 8000:8000 &
kubectl port-forward service/tu-cloud-project 8001:8001 &
kubectl port-forward service/prometheus-service 19090:9090 &

# 3. Run all experiments (~40 min)
export PYTHONIOENCODING=utf-8
bash scripts/run_all.sh
```
