# Plan to meet the project objectives (p99 < 0.5 s + outperform HPA)

Derived from the end-to-end test of `tu-cloud-project` (see `results/REPORT.md`).

## Realistic SLO stance
Memory bandwidth caps throughput beyond ~3 concurrent ResNet18 inferences. Target:
- **< 0.5 s** at baseline and in recovery;
- at the peak: scale to the useful max **and shed** the overflow (PDF allows dropping);
- **beat the HPA** on p99 / drops / demand-tracking (the graded deliverable).

## Strategy
Fastest path: **reuse the working `legacy/` bricks** (base64 inference, headless
per-pod dispatch, server-side latency metric, 3-run harness). Otherwise fix
`tu-cloud-project` in place with the phases below.

## Phase 1 — Make it correct (unblock)
1. Remove `model.half()` → fp32 (fp16 on CPU = 2-5 s/inference). `ml_model/model.py`.
2. Inference on the **sent image** (base64 bytes), not a server file path. `app.py`, `model.py`.
3. **Synchronous dispatcher**: client waits for the prediction; measure
   `receive→response` as `dispatcher_request_duration_seconds`. `dispatcher_redis.py`.
4. **Multi-replica dispatch, one in-flight per pod** (resolve the Service/headless),
   not `localhost:6001`. `dispatcher_redis.py:26`.
5. CPU `request = limit = 1`. `k8/inference-deployment.yaml`.
6. Remove emojis from prints (Windows cp1252 crash) in dispatcher / test / autoscaler.
7. CPU-only torch + bake weights + drop the top-level `cv2.imread`. `requirements.txt`,
   `Dockerfile`, `model.py`.
8. Prometheus targets via K8s service discovery (not `localhost`). `prometheus.yml`.

Exit: one request → correct prediction < 0.2 s; server-side latency metric in
Prometheus; scaling N replicas actually raises throughput.

## Phase 2 — Dispatcher: centralized queue + shedding
Short bounded queue (~3) → 503 on overflow (bounds the wait). Worker pool ≥ maxReplicas,
one in-flight/pod. Expose `dispatcher_queue_depth`, `..._dropped_total`, `..._total`.

## Phase 3 — Custom autoscaler (beat HPA)
Decide every 15 s on **queue + arrival rate + p99** (not CPU — it saturates at the
1-core cap, which is why HPA is blind). `min=1`, `max` ≈ usable cores (3-5), fast
scale-up on pressure, scale-down ~90 s (≪ HPA's 5-min default). Fix threshold 0.005→0.40.

## Phase 4 — Experiment + comparison (slide 17)
Collector every 15 s: `p99, replicas, cpu_cores, queue, drop_fraction`. Three runs on
the same `workload.txt` (custom / HPA 70 / HPA 90), one scaler at a time, reset+settle
between runs. Overlaid figures (p99, CPU, replicas, drops) + summary; 2-3 repeats for
error bars.

## Phase 5 — Tune to hold 0.5 s + document
Tune maxReplicas / queue size to stay under the contention wall; honest report of
where the SLO holds vs is shed, and the custom-vs-HPA comparison.

## Hard risks
- Memory wall: don't over-scale (raises p99); shed at the peak instead.
- Run-to-run variance: conclude on averages (repeats).
- Redis: either deploy it as a pod (manifest missing) or drop it for an in-process
  `asyncio.Queue` (as `legacy/` does).
