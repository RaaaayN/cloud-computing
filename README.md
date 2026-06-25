# Cloud Computing Project 2026

An image classification service running on Kubernetes, with our own autoscaler
that we compare against the default Kubernetes HPA.

The architecture and the details of how the autoscaler works are written up in
[AUTOSCALER.md](AUTOSCALER.md).

> The commands below are written for bash (Linux/macOS). When something is
> different on Windows, the cmd (Command Prompt) version is shown right
> underneath. Commands with no Windows note (`minikube`, `kubectl`, `docker`,
> etc.) work the same in both. On Windows use `python` instead of `python3`.

## Project layout

```
ml_model/              ResNet inference service (Flask) + Dockerfile
dispatcher/            Redis dispatcher, autoscaler and analysis scripts + Dockerfile
  autoscaler.py            our autoscaler (scaling only)
  autoscaler_logger.py     same autoscaler, but logs to CSV and draws the plots
  analyze_autoscaler_log.py  plots a single run over time
  compare_autoscalers.py     compares the custom autoscaler against the two HPA runs
  k8/                    Kubernetes manifests (apply them in the order below)
  test/test.py          load test client (reads workload.txt)
```

---

## Step 1 — Start Minikube

```bash
minikube start --cpus=4 --memory=6144
minikube addons enable metrics-server
```

Wait until the node is ready:

```bash
kubectl get nodes
```

Expected:
```
NAME       STATUS   ROLES           AGE   VERSION
minikube   Ready    control-plane   1m    v1.35.1
```

---

## Step 2 — Build Docker Images Inside Minikube

Point your terminal to Minikube's Docker daemon:

```bash
eval $(minikube docker-env)
```

Windows (cmd):

```bat
@FOR /f "tokens=*" %i IN ('minikube -p minikube docker-env --shell cmd') DO @%i
```

Build the inference image:

```bash
cd ml_model
docker build -t resnet-infer .
cd ..
```

Build the dispatcher image:

```bash
cd dispatcher
docker build -t dispatcher .
cd ..
```

Verify both images exist:

```bash
docker images | grep -E "resnet-infer|dispatcher"
```

Windows (cmd):

```bat
docker images | findstr "resnet-infer dispatcher"
```

---

## Step 3 — Deploy to Kubernetes

Apply all manifests in order:

```bash
kubectl apply -f dispatcher/k8/redis-deployment.yaml
kubectl apply -f dispatcher/k8/inference-deployment.yaml
kubectl apply -f dispatcher/k8/inference-service.yaml
kubectl apply -f dispatcher/k8/prometheus-configmap.yaml
kubectl apply -f dispatcher/k8/prometheus-deployment.yaml
kubectl apply -f dispatcher/k8/dispatcher-deployment.yaml
```

Wait for all pods to be Running:

```bash
kubectl get pods -w
```

Expected:
```
NAME                                READY   STATUS    RESTARTS   AGE
dispatcher-xxxx                     1/1     Running   0          30s
prometheus-xxxx                     1/1     Running   0          30s
redis-xxxx                          1/1     Running   0          30s
tu-cloud-project-xxxx               1/1     Running   0          30s
```

Press Ctrl+C once all pods show 1/1 Running.

---

## Step 4 — Start Port Forwards

Run in a dedicated terminal and keep it open throughout the session:

```bash
lsof -ti:5001 | xargs kill -9 2>/dev/null; true
lsof -ti:8000 | xargs kill -9 2>/dev/null; true
lsof -ti:8001 | xargs kill -9 2>/dev/null; true
lsof -ti:9090 | xargs kill -9 2>/dev/null; true
kubectl port-forward service/dispatcher-service 5001:5001 &
kubectl port-forward service/dispatcher-service 8000:8000 &
kubectl port-forward service/tu-cloud-project 8001:8001 &
kubectl port-forward service/prometheus-service 9090:9090 &
```

Windows (cmd). Free the ports, then start each forward in its own window:

```bat
for %p in (5001 8000 8001 9090) do (for /f "tokens=5" %a in ('netstat -aon ^| findstr :%p') do taskkill /F /PID %a 2>nul)
start "" kubectl port-forward service/dispatcher-service 5001:5001
start "" kubectl port-forward service/dispatcher-service 8000:8000
start "" kubectl port-forward service/tu-cloud-project 8001:8001
start "" kubectl port-forward service/prometheus-service 9090:9090
```

(Or simply open four separate terminals and run one `kubectl port-forward` in each.)

---

## Step 5 — Verify the Pipeline

Test end-to-end request flow:

```bash
curl http://localhost:5001/query \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"image": "/app/images/fire_truck.jpeg"}'
```

Windows (cmd):

```bat
curl http://localhost:5001/query -X POST -H "Content-Type: application/json" -d "{\"image\": \"/app/images/fire_truck.jpeg\"}"
```

Expected response:
```json
{"message": "Queued"}
```

Verify Prometheus is scraping both services:

```bash
curl http://localhost:9090/api/v1/targets | python3 -m json.tool | grep health
```

Windows (cmd):

```bat
curl http://localhost:9090/api/v1/targets | python -m json.tool | findstr health
```

Both targets must show "health": "up" before running any load test.

---

## Step 6 — Run Scenario A: Custom Autoscaler

Reset Redis queue and log before starting:

```bash
kubectl exec deployment/redis -- redis-cli flushall
kubectl scale deployment tu-cloud-project --replicas=1
echo "Timestamp,P99_Latency,Queue_Size,Replica_Count" > dispatcher/autoscaler_log.csv
```

Windows (cmd). Note: no quotes around the header, or they end up in the file:

```bat
kubectl exec deployment/redis -- redis-cli flushall
kubectl scale deployment tu-cloud-project --replicas=1
echo Timestamp,P99_Latency,Queue_Size,Replica_Count>dispatcher\autoscaler_log.csv
```

Terminal 1 — Start autoscaler first and wait for stable reading:

```bash
cd dispatcher
python3 autoscaler_logger.py
```

Wait until you see queue = 0 before starting the load test.

Terminal 2 — Load test:

```bash
cd dispatcher/test
python3 test.py
```

Terminal 3 — Watch pods scale:

```bash
watch -n 5 kubectl get pods
```

Windows (cmd) has no `watch`; use the built-in `-w`:

```bat
kubectl get pods -w
```

When load test reaches Second 630, stop the autoscaler with Ctrl+C:

```bash
cp dispatcher/autoscaler_log.csv dispatcher/custom_autoscaler_log.csv
echo "custom lines: $(wc -l < dispatcher/custom_autoscaler_log.csv)"
```

Windows (cmd):

```bat
copy dispatcher\autoscaler_log.csv dispatcher\custom_autoscaler_log.csv
find /c /v "" dispatcher\custom_autoscaler_log.csv
```

---

## Step 7 — Run Scenario B: HPA at 70% CPU

```bash
kubectl scale deployment tu-cloud-project --replicas=1
kubectl delete hpa tu-cloud-project 2>/dev/null; true
kubectl autoscale deployment tu-cloud-project --cpu-percent=70 --min=1 --max=10
kubectl get hpa

kubectl exec deployment/redis -- redis-cli flushall
echo "Timestamp,P99_Latency,Queue_Size,Replica_Count" > dispatcher/autoscaler_log.csv
```

Windows (cmd):

```bat
kubectl scale deployment tu-cloud-project --replicas=1
kubectl delete hpa tu-cloud-project 2>nul
kubectl autoscale deployment tu-cloud-project --cpu-percent=70 --min=1 --max=10
kubectl get hpa

kubectl exec deployment/redis -- redis-cli flushall
echo Timestamp,P99_Latency,Queue_Size,Replica_Count>dispatcher\autoscaler_log.csv
```

Terminal 1:
```bash
cd dispatcher && python3 autoscaler_logger.py
```

Terminal 2:
```bash
cd dispatcher/test && python3 test.py
```

When done:
```bash
cp dispatcher/autoscaler_log.csv dispatcher/hpa70_log.csv
kubectl delete hpa tu-cloud-project
```

Windows (cmd):

```bat
copy dispatcher\autoscaler_log.csv dispatcher\hpa70_log.csv
kubectl delete hpa tu-cloud-project
```

---

## Step 8 — Run Scenario C: HPA at 90% CPU

```bash
kubectl scale deployment tu-cloud-project --replicas=1
kubectl autoscale deployment tu-cloud-project --cpu-percent=90 --min=1 --max=10
kubectl get hpa

kubectl exec deployment/redis -- redis-cli flushall
echo "Timestamp,P99_Latency,Queue_Size,Replica_Count" > dispatcher/autoscaler_log.csv
```

Windows (cmd):

```bat
kubectl scale deployment tu-cloud-project --replicas=1
kubectl autoscale deployment tu-cloud-project --cpu-percent=90 --min=1 --max=10
kubectl get hpa

kubectl exec deployment/redis -- redis-cli flushall
echo Timestamp,P99_Latency,Queue_Size,Replica_Count>dispatcher\autoscaler_log.csv
```

Terminal 1:
```bash
cd dispatcher && python3 autoscaler_logger.py
```

Terminal 2:
```bash
cd dispatcher/test && python3 test.py
```

When done:
```bash
cp dispatcher/autoscaler_log.csv dispatcher/hpa90_log.csv
kubectl delete hpa tu-cloud-project
```

Windows (cmd):

```bat
copy dispatcher\autoscaler_log.csv dispatcher\hpa90_log.csv
kubectl delete hpa tu-cloud-project
```

---

## Step 9 — Generate Comparison Plots

```bash
cd dispatcher
python3 compare_autoscalers.py custom_autoscaler_log.csv hpa70_log.csv hpa90_log.csv
python3 analyze_autoscaler_log.py
```

Open the generated plots:

```bash
open comparison_plot.png
open autoscaler_performance_plot.png
open p99_latency_plot.png
open queue_size_plot.png
open replica_count_plot.png
```

Windows (cmd): use `start` instead of `open`, e.g. `start comparison_plot.png`.

---

## Pre-recorded Results

We left the files from one full run in `dispatcher/` so the results can be looked at
without re-running everything.

The per-run plots come from `autoscaler_logger.py`, `comparison_plot.png` from
`compare_autoscalers.py`, and `autoscaler_performance_plot.png` from
`analyze_autoscaler_log.py`. All of them land in the current directory.

To just redraw the comparison from the saved logs:

```bash
cd dispatcher
python3 compare_autoscalers.py custom_autoscaler_log.csv hpa70_log.csv hpa90_log.csv
```

---

## Autoscaling Logic

The autoscaler checks the queue and P99 latency every 15 seconds and scales from
there. The full rule table and the reasoning behind it are in [AUTOSCALER.md](AUTOSCALER.md).

---

## Prometheus Queries

Open http://localhost:9090 and run:

| Metric | Query |
|---|---|
| P99 Latency | histogram_quantile(0.99, rate(inference_latency_seconds_bucket[1m])) |
| Queue Size | dispatcher_queue_size |
| Total Requests | dispatcher_requests_total |
| Forwarded Requests | dispatcher_requests_forwarded |

---

## Kubernetes Resources

| Component | CPU Request | CPU Limit | Notes |
|---|---|---|---|
| ML Inference Pod | 1 core | 1 core | As per project requirement |
| Dispatcher | default | default | Single replica |
| Redis | default | default | Single replica |
| Prometheus | default | default | Single replica |