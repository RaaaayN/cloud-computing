# Test report — `tu-cloud-project` (custom autoscaler vs HPA)

End-to-end test of the project on Minikube + Docker, following the README, then a
3-run comparison (custom autoscaler vs HPA 70 % vs HPA 90 %) on the same workload.

## Setup actually run
- **Minikube** (Docker driver) + metrics-server.
- Model image `resnet-infer` built in Minikube's Docker (README says `inference-model`
  — name mismatch with the manifest; built the manifest's name).
- Inference pod (`tu-cloud-project`), port-forward `6001/8001`.
- **Redis** (Docker), **Prometheus** (Docker, scraping dispatcher :8000 + inference :8001).
- Dispatcher, autoscaler (`autoscaler_logger.py`) and load generator run locally.
- Workload: baseline (5 rps, 30 s) → burst (30 rps, 60 s) → baseline (5 rps, 30 s).
- Metrics sampled every 15 s: server p99 (`inference_latency_seconds`), replica count,
  CPU cores (`kubectl top`), queue size, drops. CSVs: `custom.csv`, `hpa70.csv`,
  `hpa90.csv`; figures `cmp_*.png`; `summary.csv`.

## Workarounds needed just to start (bugs)
1. Dispatcher crashes on Windows — emoji `🚀` (`dispatcher_redis.py:91`) → `PYTHONIOENCODING=utf-8`.
2. `test.py` crashes — emoji `⏱️` (line 29) → same; used an equivalent loader.
3. `autoscaler_logger.py` crashes on scale — `✓` (line 64) → same.
4. README `prometheus.yml` targets `localhost` → unreachable from a container; used
   `host.docker.internal` (and the README's volume mount does not load on Docker Windows).
5. `requirements.txt` not pinned to CPU torch → pip pulls full CUDA stack (huge image).

## Results

| Metric | custom | HPA 70 % | HPA 90 % |
|---|---|---|---|
| p99 inference (median / max) | 10.0 / 10.0 s | 10.0 / 10.0 s | 10.0 / 10.0 s |
| Max replicas | 10 | 10 | 7 |
| Max CPU cores (with up to 10 pods) | 1.81 | 1.81 | 1.53 |
| Requests dropped | **98.0 %** | 98.3 % | 98.7 % |

p99 = 10 s is the histogram's top bucket (real value ≥ 10 s, i.e. ≥ 20× the 0.5 s SLO).

## Conclusion
- **The custom autoscaler does NOT outperform the HPA** — all three are identical at
  the failure ceiling (p99 ≈ 10 s, ~98 % dropped). The PDF objective "outperform HPA"
  is not met.
- **Scaling is inoperative by design.** All three scale to 7–10 replicas, yet total CPU
  plateaus at ~1.8 cores (~0.18 core/replica) → ~9 pods sit idle. The dispatcher
  forwards only to `localhost:6001` (`dispatcher_redis.py:26`) + a single port-forward,
  so added replicas never receive traffic.
- **fp16 on CPU** (`model.half()`, `model.py:21`) makes one inference ~2–5 s on CPU
  (vs ~0.05 s in fp32), so the 0.5 s SLO is unreachable even with one request.
- Plus: path-based inference (always the same local image), fire-and-forget dispatcher
  (no server-side latency measured), CPU `request 500m ≠ limit 1`.

The project starts (after the workarounds above) but meets none of the PDF performance
objectives. See the prior `legacy/` implementation for a compliant baseline.
