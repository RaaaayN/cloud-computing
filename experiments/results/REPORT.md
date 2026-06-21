# Elastic ML Inference Serving — Autoscaling Results

Comparison of a **custom queue/SLO-driven autoscaler** against the **Kubernetes
Horizontal Pod Autoscaler (HPA)** at 70 % and 90 % CPU targets, for a ResNet18
image-classification inference service on Minikube.

> One campaign, 3 back-to-back runs replaying the same `workload.txt` trace.
> CSVs: `custom.csv`, `hpa70.csv`, `hpa90.csv` (columns include `drop_fraction`).
> Figures: `comparison_p99.png`, `comparison_cpu.png`, `comparison_replicas.png`,
> `comparison_queue.png`. Reproduce with `pwsh ./scripts/run_all.ps1`.

---

## 1. System (and PDF compliance)

| Component | Implementation |
|---|---|
| Load tester | Replays `workload.txt` (~7 rps baseline → ~30 rps burst → baseline), real ImageNet images. |
| Dispatcher | Centralized bounded queue (size 3). **Headless per-pod dispatch: exactly one in-flight request per replica** → replicas never queue internally (PDF slide 21). Overflow is shed as `503`. |
| Inference replicas | aiohttp + ResNet18 on **CPU**, `cpu request = limit = 1`, threads pinned to 1 (OMP/MKL), one inference at a time. ResNet18 weights baked into the image (fast startup). |
| Monitoring | Prometheus, per-pod scraping, every 15 s. |
| Autoscaler | Custom: decides every 15 s from **queue depth + arrival rate + p99** (not CPU%). `min/max = 1/3`, fast-scale on `p99>0.4` or `queue>3`, ~90 s scale-down. |

**Graded metric — server-side latency.** p99 = `dispatcher_request_duration_seconds`
= queue wait + inference.

---

## 2. Results (3 runs, same workload)

| Metric | **custom** | HPA 70 % | HPA 90 % |
|---|---|---|---|
| Steady-state p99 | 0.19 s ✅ | 0.19 s ✅ | 0.19 s ✅ |
| Median p99 | 0.20 s | 0.19 s | 0.19 s |
| Peak p99 | 0.83 s | 0.56 s | 1.27 s |
| Samples p99 > 0.5 s | 11/41 | 2/42 | 6/42 |
| Max replicas | 3 | 3 | 3 |
| Mean / peak CPU cores | 1.12 / 2.9 | 0.86 / 2.0 | 0.83 / 2.9 |
| **Drop fraction (mean / max)** | **7.7 % / 38 %** | 11.1 % / 51 % | 12.2 % / 50 % |

---

## 3. What the custom autoscaler wins — and what it does not

**It does NOT beat HPA on peak p99.** This is the honest headline. With a short,
shedding queue, an under-provisioned service drops the overflow instead of queuing
it, so HPA keeps a *low* p99 on the requests it does serve. The custom, by serving
more, carries more borderline requests and shows a slightly higher p99.

**It wins, robustly, on three axes:**

1. **Availability / drops** — the custom sheds **7.7 %** vs **11–12 %** for HPA
   (peak 38 % vs ~50 %). It scales to serve the load instead of dropping it.
2. **Demand tracking** — replicas and CPU follow the workload (1 → 3 → 1); both HPA
   runs sit flat near the floor (see `comparison_replicas.png`).
3. **Responsiveness** — it scales up on a *leading* signal (queue/arrival) ahead of
   HPA's lagging CPU signal, and scales **down in ~90 s** vs HPA's **default 5-min**
   scale-down stabilization (faster core release on recovery).

This is exactly the course's elasticity story (slide 15): the custom matches
*required* capacity; HPA under-provisions.

---

## 4. Why neither holds < 0.5 s at the sustained peak (the real wall)

**Scaling does not increase throughput on this hardware.** Beyond ~3 concurrent
ResNet18 inferences — each pinned to 1 CPU — the shared **memory bandwidth**
saturates, so every inference slows (~0.08 s uncontended → ~0.8 s under load).
Adding replicas then buys almost no extra throughput; we measured this repeatedly
(5 and 10 replicas gave *higher* p99, not lower). So at the ~30 rps peak the
service is capacity-bound regardless of the autoscaler, and the bounded queue
sheds the overflow. The SLO is met at baseline and in recovery; the sustained peak
exceeds it for **all three** autoscalers. The custom's job there is to scale to the
useful maximum (3) and shed gracefully — which it does.

---

## 5. Honest caveats

- **Run-to-run variance is large.** Across campaigns HPA 70 % showed 23 / 15 / 2
  samples over 0.5 s for the *same* config — workload/scheduling jitter dominates a
  single run's p99. The **drops** and **demand-tracking** advantages are stable;
  the p99 ranking is not. Error bars (repeated runs) would be needed to rank p99.
- **Node sizing.** Minikube runs with 16 CPUs (laptop has 18); all three runs share
  the identical cluster, so the comparison is fair.
- **Tuning is calibrated to the given `workload.txt`.** The policy reacts to generic
  signals (no hardcoded timings), but `REPLICA_MAX`, queue size and headroom were
  chosen for this trace.

---

## 6. Reproducibility

```powershell
pwsh ./scripts/install.ps1 -Cpus 16 -Memory 14g
pwsh ./scripts/run_all.ps1
```

Key settings: inference `cpu = 1` (request = limit), threads pinned to 1, ResNet18
weights baked in; custom autoscaler `INTERVAL_SEC=15`, `min/max = 1/3`,
`MAX_DELTA_PER_CYCLE=5`, `S_WARN=0.40`, `SERVICE_TIME=0.1`, `COOLDOWN_CYCLES=6`;
dispatcher headless per-pod dispatch, queue max = 3.
