# Experiment harness — custom autoscaler vs HPA 70/90

Reproduces the slide-17 comparison: 3 runs on the same load profile, comparing
p99 inference latency and CPU cores used.

## Install tooling
```bash
pip install -r experiments/requirements.txt
```

## During each run
Port-forward Prometheus, then sample metrics to a CSV:
```bash
kubectl -n inference-system port-forward svc/prometheus 9090:9090 &
python experiments/collect.py --out custom.csv      # run 1: custom autoscaler
python experiments/collect.py --out hpa70.csv       # run 2: HPA 70%
python experiments/collect.py --out hpa90.csv       # run 3: HPA 90%
```
`collect.py` records `timestamp, p99_latency, replica_count, cpu_cores` every 15 s.
- p99 comes from Prometheus.
- replica_count and cpu_cores come from the Kubernetes API / metrics-server
  (the Prometheus config scrapes Service DNS, so it cannot count replicas).

Stop it with Ctrl-C when the load-tester Job finishes (or pass `--duration`).

## Run sequencing (one scaler at a time!)
```bash
# Run 1 — custom autoscaler (remove --dry-run from k8s/autoscaler-deployment.yaml first)
kubectl -n inference-system delete hpa --all
kubectl apply -f k8s/autoscaler-deployment.yaml
kubectl apply -f k8s/loadtester-job.yaml

# Run 2 — HPA 70%
kubectl -n inference-system delete deploy custom-autoscaler
kubectl -n inference-system scale deploy/inference --replicas=1
kubectl apply -f k8s/hpa-70.yaml
kubectl apply -f k8s/loadtester-job.yaml

# Run 3 — HPA 90%
kubectl -n inference-system delete hpa inference-hpa
kubectl apply -f k8s/hpa-90.yaml
kubectl apply -f k8s/loadtester-job.yaml
```

## Produce the figures
```bash
python experiments/plot.py custom.csv hpa70.csv hpa90.csv --out-prefix comparison
# -> comparison_p99.png, comparison_cpu.png
```
