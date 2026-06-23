# Cloud Computing Project 2026
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

---

## Step 5 — Verify the Pipeline

Test end-to-end request flow:

```bash
curl http://localhost:5001/query \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"image": "/app/images/fire_truck.jpeg"}'
```

Expected response:
```json
{"message": "Queued"}
```

Verify Prometheus is scraping both services:

```bash
curl http://localhost:9090/api/v1/targets | python3 -m json.tool | grep health
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

When load test reaches Second 630, stop the autoscaler with Ctrl+C:

```bash
cp dispatcher/autoscaler_log.csv dispatcher/custom_autoscaler_log.csv
echo "custom lines: $(wc -l < dispatcher/custom_autoscaler_log.csv)"
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

---

## Step 8 — Run Scenario C: HPA at 90% CPU

```bash
kubectl scale deployment tu-cloud-project --replicas=1
kubectl autoscale deployment tu-cloud-project --cpu-percent=90 --min=1 --max=10
kubectl get hpa

kubectl exec deployment/redis -- redis-cli flushall
echo "Timestamp,P99_Latency,Queue_Size,Replica_Count" > dispatcher/autoscaler_log.csv
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

---

## Pre-recorded Results

The following files are included from a completed run:

| File | Description |
|---|---|
| custom_autoscaler_log.csv | Custom autoscaler run log |
| hpa70_log.csv | HPA 70% CPU run log |
| hpa90_log.csv | HPA 90% CPU run log |
| results/autoscaler_p99_latency_plot.png | P99 latency over time |
| results/autoscaler_queue_size_plot.png | Queue size over time |
| results/autoscaler_replica_count_plot.png | Replica count over time |

To regenerate comparison without re-running experiments:

```bash
cd dispatcher
python3 compare_autoscalers.py custom_autoscaler_log.csv hpa70_log.csv hpa90_log.csv
```

---

## Autoscaling Logic

The custom autoscaler runs every 15 seconds and uses queue depth as its primary signal:

| Condition | Action |
|---|---|
| Queue > 200 | Scale up by 3 replicas |
| Queue > 50 | Scale up by 2 replicas |
| Queue > 10 OR P99 > 0.4s | Scale up by 1 replica |
| Queue = 0 AND P99 < 0.3s | Scale down by 1 replica |
| Otherwise | No change |

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

## Troubleshooting

**Pods stuck in Pending or ImagePullBackOff:**
```bash
kubectl describe pod <pod-name>
```
Most common cause: forgot to run eval $(minikube docker-env) before building images.

**Prometheus targets showing DOWN:**
```bash
kubectl logs deployment/prometheus --tail=20
```
Restart port-forwards if they died.

**Autoscaler shows N/A for all metrics:**
```bash
curl http://localhost:9090/api/v1/targets | python3 -m json.tool | grep health
```
Both targets must be UP before metrics appear.

**Stale latency values with empty queue:**
```bash
kubectl exec deployment/redis -- redis-cli flushall
kubectl scale deployment tu-cloud-project --replicas=1
```
Wait 2 minutes for Prometheus to clear old histogram data.

**Port already in use:**
```bash
lsof -ti:<port> | xargs kill -9
```

---

## Kubernetes Resources

| Component | CPU Request | CPU Limit | Notes |
|---|---|---|---|
| ML Inference Pod | 1 core | 1 core | As per project requirement |
| Dispatcher | default | default | Single replica |
| Redis | default | default | Single replica |
| Prometheus | default | default | Single replica |