# TU Cloud Project – Scalable Image Classification with Autoscaling

This project implements a cloud-based image inference system using Docker, Kubernetes, Redis, Prometheus, and Flask. It supports both custom autoscaling and Kubernetes HPA, and evaluates performance under varying workloads.

## 🚀 Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/SatyaDewangan05/tu-cloud-project.git
cd tu-cloud-project
```

---

### 2. Build and Run Locally (Without Kubernetes)

From ml_model folder:

```bash
cd ml_model
python3 -m venv ml_pipe
source ml_pipe/bin/activate
pip install -r requirements.txt
python app.py  # Should run on port 6001
```

In another terminal, run dispatcher:

```bash
cd dispatcher
python3 -m venv venv_disp
source venv_disp/bin/activate
pip install -r ../ml_model/requirements.txt
python dispatcher_redis.py  # Should run on port 5001
```

---

### 3. Build and Deploy to Minikube

Start Minikube with metrics-server:

```bash
minikube start --addons=metrics-server
```

Build and push Docker image:

```bash
eval $(minikube docker-env)
cd ml_model
docker build -t inference-model .
```

Deploy Kubernetes resources:

```bash
cd ../dispatcher/k8
kubectl apply -f inference-deployment.yaml
kubectl apply -f inference-service.yaml
```

Expose service:

```bash
minikube service tu-cloud-project
kubectl port-forward deployment/tu-cloud-project 6001:6001 8001:8001
```

---

### 4. Start Prometheus

Ensure prometheus.yml is in dispatcher folder.

Start Prometheus (optional Docker version):

```bash
docker run -p 9090:9090     -v $(pwd)/prometheus.yml:/etc/prometheus/prometheus.yml     prom/prometheus
```

---

## 📈 Autoscaler Usage

Run this from dispatcher:

```bash
python autoscaler_logger.py
```

To analyze:

```bash
python analyze_autoscaler_log.py
```

This generates:

- autoscaler_summary.csv
- p99_latency_plot.png
- queue_size_plot.png
- replica_count_plot.png

---

## 🧪 Load Testing

Prepare your image inside ml_model/images/fire_truck.jpeg

Run test from dispatcher:

```bash
cd dispatcher/test
python test.py
```

Use workload.txt or workload_heavy.txt to simulate different RPS loads.

---

## 🤖 Kubernetes HPA Setup

Deploy HPA with CPU target:

```bash
kubectl autoscale deployment tu-cloud-project --cpu-percent=70 --min=1 --max=10
```

Use watch to observe:

```bash
watch -n 5 kubectl get hpa
```

Run your workload while HPA is active and collect stats.

---

## 📊 Compare Autoscaler vs HPA

After running autoscaler_logger.py and gathering logs:

```bash
python analyze_autoscaler_log.py
```

Compare with metrics collected using HPA (export using kubectl top pods and log CPU usage).

---

## ✅ Goals

- Achieve server-side latency < 0.5s
- Demonstrate autoscaler responsiveness under load
- Compare HPA (70%, 90%) vs custom autoscaler

---

## 📬 Contact

For any help, reach out to your project supervisor or the contributor.
