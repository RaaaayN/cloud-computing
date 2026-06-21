# Test report — `tu-cloud-project` (custom autoscaler vs HPA)

End-to-end test on Minikube + Docker, then the 3-run comparison (custom vs HPA 70 %
vs HPA 90 %) on the same workload. Two states are recorded: **before** the fixes
(as received) and **after** Phase 1-3 fixes.

## A. As received — every objective failed
Run end-to-end via the README's Docker/Minikube path, the project required 5
workarounds just to start (3 Windows emoji crashes, `prometheus.yml` on `localhost`,
CUDA torch instead of CPU) and then:
- p99 ≈ **10 s** (≥20× the 0.5 s SLO), **~98 % dropped**, for custom *and* both HPAs.
- Root causes: `model.half()` (fp16 on CPU → 2-5 s/inference); the dispatcher forwarded
  only to `localhost:6001` (one pod), so scaling to 10 replicas left ~9 idle and
  changed nothing; fire-and-forget dispatch (no server-side latency measured);
  path-based inference; Flask bound to 127.0.0.1; CPU `request 500m ≠ limit 1`.

## B. After Phase 1-3 fixes
Fixes: fp32 CPU inference (~0.12 s); base64 image upload; synchronous dispatcher that
returns the prediction and records server-side latency `dispatcher_request_duration_seconds`;
dispatcher containerized and run **in-cluster** so it reaches all replicas via the
Service (kube-proxy LB) → scaling now raises throughput; Flask on 0.0.0.0; CPU=1/1;
CPU-only torch + baked weights; emojis removed; autoscaler scales on server-side p99 +
queue (threshold 0.40, not 0.005).

Workload: baseline 5 rps → burst 30 rps → baseline 5 rps. Metrics every 15 s.

| Metric | **custom** | HPA 70 % | HPA 90 % |
|---|---|---|---|
| p99 (median / max) | **0.97 / 1.11 s** | 4.05 / 4.60 s | n/a* |
| Max replicas | 6 | 10 | 8 |
| Max CPU cores | 0.54 | 0.94 | 0.76 |
| Drops | 40.5 % | 51.1 % | n/a* |

\* hpa90 run: the dispatcher stopped forwarding mid-run (collection glitch) — to be re-run.

**The custom autoscaler outperforms the HPA** (PDF objective met): it holds p99 ≈ 1 s
where HPA 70 % climbs to **4.6 s**, using **fewer replicas (6 vs 10) and less CPU**.
The custom scales on queue + server p99 (leading signals) and reacts before HPA, whose
CPU signal lags → backlog → high p99.

## C. Honest caveats / remaining work
- **< 0.5 s not yet met at the 30 rps burst** (p99 ≈ 1 s, ~40 % drops). The bottleneck
  is now the **single synchronous Flask dispatcher** (GIL + 130 KB JSON re-serialisation
  per request). Phase 2 lever: async dispatcher (one-in-flight/pod) or scale the
  dispatcher. SLO is met at baseline (~0.2 s).
- Metric series have `nan` gaps (the p99 `[1m]` window empties during the 20 s settle
  between runs) and `kubectl port-forward` is fragile over a long run (now wrapped in a
  reconnect loop). For a clean graded figure: repeat runs + error bars, and re-run hpa90.
- Node: Minikube on Docker/Windows; all runs share the same cluster.

Figures: `cmp_p99.png`, `cmp_replicas.png`, `cmp_cpu.png`, `cmp_drops.png`. CSVs:
`custom.csv`, `hpa70.csv`, `hpa90.csv`. Plan: `PLAN.md`.
