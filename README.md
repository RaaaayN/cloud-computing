# Cloud Project – Scalable Image Classification with Autoscaling

This project implements a cloud-based image inference system using Docker, Kubernetes, Redis, Prometheus, and Flask. It supports both custom autoscaling and Kubernetes HPA, and evaluates performance under varying workloads.

## 🚀 Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/RaaaayN/cloud-computing.git
cd cloud-computing
```

---

### 2. Build and Run Locally (Without Kubernetes)

From inference-service folder:

```bash
cd inference-service
python3 -m venv inference-service
source inference-service/bin/activate
pip install -r requirements.txt
python app.py 

test: 

curl -X POST http://localhost:6001 \
-H "Content-Type: application/json" \
-d '{"image":"images/cat.jpg"}'

#For windows

Invoke-RestMethod `
  -Uri "http://localhost:6001" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"image":"images/cat.jpg"}'



In another terminal, run dispatcher:

```bash
cd dispatcher
python3 -m venv venv_disp
source venv_disp/bin/activate
pip install -r ../inference-service/requirements.txt
python dispatcher_redis.py  #  runs on port 5001

#start redis on another terminal
docker start redis
docker ps

test :

curl -X POST http://localhost:5001/query \
-H "Content-Type: application/json" \
-d '{"image":"images/cat.jpg"}'

#for windows
Invoke-RestMethod `
  -Uri "http://localhost:5001/query" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"image":"images/cat.jpg"}'


```



### 3. Build and Deploy to Minikube

Start Minikube with metrics-server:

```bash
minikube start --addons=metrics-server
```

Build and push Docker image:

```bash
eval $(minikube docker-env)
cd inference-service
docker build -t inference-model .
```

Deploy Kubernetes resources:

```bash
cd ../k8s-manifest
kubectl apply -f inference-deployment.yaml
kubectl apply -f inference-service.yaml
```

Expose service:

```bash
minikube service cloud-project
kubectl port-forward deployment/cloud-project 6001:6001 8001:8001
```

---

### 4. Start Prometheus

Ensure prometheus.yml is in dispatcher folder.

Start Prometheus (optional Docker version):
cd dispatcher  
```bash
docker run -p 9090:9090     -v $(pwd)/prometheus.yml:/etc/prometheus/prometheus.yml     prom/prometheus
```

---

## 📈 Autoscaler Usage
run test.py in t
Run this from dispatcher:
cd dispatcher
```bash
python autoscaler_logger.py
```

Compare with metrics collected using HPA (export using kubectl top pods and log CPU usage).

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