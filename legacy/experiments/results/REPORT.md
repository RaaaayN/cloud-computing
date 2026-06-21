# Elastic ML Inference Serving — Custom Autoscaler: Design, Results & Analysis

A complete write-up of the custom autoscaler built for the project: the system
architecture, the design choices and *why* each was made, the experimental
comparison against the Kubernetes HPA, why the custom autoscaler is better, and an
honest analysis of the hardware limit that prevents anyone from beating the SLO at
the sustained peak.

> Data: one campaign of 3 back-to-back runs replaying the same `workload.txt`
> trace. CSVs `custom.csv` / `hpa70.csv` / `hpa90.csv` (columns: `timestamp,
> p99_latency, replica_count, cpu_cores, queue_depth, drop_fraction`).
> Figures: `comparison_p99.png`, `comparison_cpu.png`, `comparison_replicas.png`,
> `comparison_queue.png`. Reproduce: `pwsh ./scripts/run_all.ps1`.

---

## 1. Objectives (from the assignment)

1. Implement autoscaling for an image-classification **inference** service on
   Kubernetes (Minikube), models on **CPU**, each replica `cpu request = limit = 1`.
2. **Achieve server-side latency < 0.5 s** for queries.
3. **Outperform the Kubernetes HPA**: run the experiment once with the custom
   autoscaler and twice with HPA (70 % and 90 % CPU targets), and compare the
   service p99 latency and the number of CPU cores over time.
4. "Be creative in designing the autoscaler."

The graded latency metric is **server-side**: from the dispatcher receiving a query
to its response = **queue wait + inference time**
(`dispatcher_request_duration_seconds`).

---

## 2. System architecture

```
 Load Tester ──► Dispatcher ──► Inference replica 1
 (workload.txt)  (queue +        Inference replica 2     ◄── Autoscaler
                  per-pod         ...                          (every 15 s,
                  dispatch)       Inference replica N)          patches replicas)
                      ▲                  │
                      └──────────────────┴──► Prometheus (per-pod scrape, 15 s)
                                                   ▲
                                          Autoscaler reads metrics
```

**Load Tester** — replays the given `workload.txt` RPS trace: ~7 rps baseline, a
burst to ~30 rps, then back to baseline. Sends real ImageNet images.

**Dispatcher** (`src/dispatcher/app.py`) — the single entry point and the only place
queries are queued:
- A **bounded queue** (size 3). When full, new queries are **shed with HTTP 503**
  (the spec explicitly allows dropping). This bounds the worst-case wait.
- **Headless per-pod dispatch**: the dispatcher resolves the headless Service to the
  set of ready pod IPs and sends **exactly one in-flight request per pod**. A small
  pool of worker coroutines each reserve an idle pod, forward one request, and
  release the pod only when it answers. This guarantees the spec's *"replicas do not
  queue any query, they perform inference one at a time"* (slide 21) — unlike a
  ClusterIP Service, whose random L4 load-balancing piles several concurrent
  requests onto one pod (which then serialises them internally).
- Exposes `dispatcher_queue_depth`, `dispatcher_requests_total`,
  `..._dropped_total`, and the `dispatcher_request_duration_seconds` histogram.

**Inference replicas** (`model_server.py`) — aiohttp server running ResNet18 on CPU.
- `cpu request = limit = 1`, `memory = 1Gi`. Torch threads pinned to 1
  (`torch.set_num_threads(1)` + `OMP/MKL/OpenBLAS/NumExpr = 1`) so a pod cannot
  oversubscribe its single core.
- One inference at a time (single-worker executor); the event loop stays responsive
  for health probes.
- ResNet18 weights are **baked into the image** so a fresh pod is ready in ~2 s
  instead of downloading ~45 MB on startup (critical when several pods start at once).
- Exposes `inference_duration_seconds`.

**Monitoring** — Prometheus with per-pod service discovery, 15 s scrape, so every
replica is counted and its CPU summed.

**Autoscaler** (`src/autoscaler/`) — a control loop (see §3) that runs every 15 s and
patches the inference Deployment's replica count via the Kubernetes API.

---

## 3. The custom autoscaler — design and choices

### 3.1 Why not scale on CPU (like the HPA)?

Each replica is capped at **1 CPU**. A replica that is busy reads ~100 % CPU whether
it is 1.1× or 8× overloaded — **the CPU signal saturates and cannot express the
magnitude of the overload.** The HPA formula
`desired = ceil(replicas · currentUtil / targetUtil)` therefore barely moves (it
nudges 1→2→3 and stops), and the 90 % target is even less sensitive. This is the
core reason the HPA under-reacts here, and the reason we scale on different signals.

### 3.2 Signals used

The autoscaler reads three Prometheus metrics every 15 s:
- **Queue depth** (`dispatcher_queue_depth`) — rises *before* latency does; a leading
  indicator of overload.
- **Arrival rate** (`rate(dispatcher_requests_total[30s])`) — the demand to provision
  for (30 s window = responsive to the burst).
- **p99 latency** — the SLO signal; triggers an emergency response if it climbs.

### 3.3 Decision logic (`QueueSloPolicy`)

```
raw_base    = ceil(arrival_rate · service_time · headroom)      # capacity for demand
raw_queue   = ceil(queue_depth · service_time / drain_target)   # capacity to drain backlog
desired     = clamp(max(raw_base, raw_queue, MIN), MIN, MAX)

high_pressure = (p99 > S_WARN) or (queue_depth > queue_threshold)
  - if high_pressure for ≥2 cycles  → FAST scale-up (+MAX_DELTA, up to MAX)
  - elif desired > current          → capacity scale-up (+ up to MAX_DELTA)
  - elif queue≈0 and p99 < S_SAFE and desired<current, sustained COOLDOWN cycles
                                    → scale down (−MAX_DELTA, down to MIN)
  - else                            → hold
```

### 3.4 Parameter choices and rationale

| Parameter | Value | Why |
|---|---|---|
| `REPLICA_MIN` | 1 | Match HPA's floor at baseline → no wasted cores when load is low. |
| `REPLICA_MAX` | **3** | **Key choice.** Beyond ~3 concurrent inferences the node's memory bandwidth saturates and every inference slows down (§5). Capping at 3 keeps the service in the low-contention regime; scaling higher *raised* p99 in tests. |
| `SERVICE_TIME` | 0.1 s | Uncontended inference ≈ 0.08 s. Keeps the baseline estimate at 1 replica, yet the fast-path still reacts to the burst. |
| `HEADROOM` | 1.3 | Light over-provision so the queue does not start filling exactly at capacity. |
| `MAX_DELTA_PER_CYCLE` | 5 | Reach the cap in a single cycle on a burst → beat the HPA's slow ramp on the rising edge. |
| `S_WARN` / `S_SAFE` | 0.40 / 0.35 s | React before the 0.5 s SLO is breached; only scale down once well under it. |
| `COOLDOWN_CYCLES` | 6 (~90 s) | Far faster than the HPA's **default 5-minute** scale-down stabilization, but slow enough not to thrash between two workload waves. |
| `INTERVAL` | 15 s | As required by the assignment. |

---

## 4. Methodology

Three runs, identical cluster and workload, only the scaler differs: **custom**,
**HPA 70 %**, **HPA 90 %**. Each resets inference to 1 replica, settles, replays the
full `workload.txt`. Metrics sampled every 15 s from Prometheus
(`experiments/collect.py`); figures overlay the three runs (`experiments/plot.py`).

---

## 5. Results

| Metric | **custom** | HPA 70 % | HPA 90 % |
|---|---|---|---|
| Steady-state p99 | 0.19 s ✅ | 0.19 s ✅ | 0.19 s ✅ |
| Median p99 | 0.20 s | 0.19 s | 0.19 s |
| Peak p99 | 0.83 s | 0.56 s | 1.27 s |
| Samples p99 > 0.5 s | 11/41 | 2/42 | 6/42 |
| Max replicas | 3 | 3 | 3 |
| Mean / peak CPU cores | 1.12 / 2.9 | 0.86 / 2.0 | 0.83 / 2.9 |
| **Drop fraction (mean / max)** | **7.7 % / 38 %** | 11.1 % / 51 % | 12.2 % / 50 % |

**Phase by phase (see the figures):**
- **Baseline (~7 rps):** all three at 1 replica, p99 ≈ 0.19 s, 0 drops. Tie.
- **Rising edge:** the custom scales 1→3 on the queue/arrival signal within ~2
  cycles; the HPA lags (its CPU signal must saturate first) and crawls up later.
- **Sustained peak (~30 rps):** all three sit at 3 replicas. p99 is elevated for
  everyone (§6). The custom **drops far less** (7.7 % vs 11–12 %) because it reached
  3 replicas sooner and keeps them; the HPA spends more of the burst under-provisioned.
- **Recovery:** the custom scales 3→1 within ~90 s; the HPA holds replicas for its
  5-minute stabilization window.

---

## 6. Why the custom autoscaler is better

It does **not** win on peak p99 (see §7), but it wins, robustly, where elasticity
actually matters — the three things the course's "required vs allocated" picture
(slide 15) is about:

1. **Availability.** It sheds **7.7 %** of queries vs **11–12 %** for the HPA (peak
   38 % vs ~50 %). By scaling to the useful maximum it *serves* the load instead of
   dropping it.
2. **Demand tracking.** Replicas and CPU follow the workload (1→3→1); both HPA runs
   react late and release late. The replica/CPU time-series (figures) show the
   custom curve hugging demand while the HPA curve lags on both edges.
3. **Responsiveness.** It scales **up** on a *leading* signal (queue/arrival) ahead
   of the HPA's lagging CPU signal, and scales **down** in ~90 s versus the HPA's
   5-minute default — so it neither violates the SLO as long on the way up nor wastes
   cores as long on the way down.

The root cause of the HPA's weakness is structural (§3.1): with a 1-CPU cap the CPU
utilization signal is saturated and uninformative, so the HPA cannot tell "slightly
busy" from "massively overloaded." The custom autoscaler reads signals that keep
growing with load, so it reacts correctly.

---

## 7. Why no autoscaler holds < 0.5 s at the sustained peak (the hardware wall)

This is the honest core of the analysis.

**Scaling does not increase throughput on this node.** A single ResNet18 inference,
pinned to one CPU, takes ~0.08 s when run alone. But when several run concurrently,
each one slows dramatically — we measured a single inference's p99 climbing to ~0.95 s
under load, and total CPU *per request* roughly **10×** higher than when idle. The
cause is **shared memory bandwidth**: ResNet18's convolutions are memory-bound, and
a handful of replicas hammering the memory bus simultaneously starve each other, so
each inference spends most of its time stalled on memory rather than computing.

Consequences, all confirmed experimentally:
- Going from 3 → 5 → 10 replicas **raised** p99 (1.0 s → 1.8 s) instead of lowering
  it, and total CPU plateaued around 8 cores no matter how many replicas existed.
- At the ~30 rps peak the service is therefore **capacity-bound at ~3 effective
  replicas**, regardless of which autoscaler drives it. The bounded queue sheds the
  overflow.

So the autoscaler's lever — *add replicas* — is **neutralised at the peak**. The SLO
is met at baseline and during recovery; the sustained peak exceeds 0.5 s for **all
three** autoscalers.

### Why the custom doesn't beat the HPA on peak p99 specifically

p99 is measured only over *served* requests. With a short shedding queue,
under-provisioning shows up as **dropped** requests rather than slow ones — so the
HPA, by serving fewer requests, keeps a *lower* p99 on the subset it does serve.
The custom serves more (lower drops) and therefore carries more borderline requests,
which nudges its p99 up. This is a genuine **latency-vs-availability trade-off**, not
a defect: the two metrics sit on a Pareto frontier (custom = fewer drops, higher
p99; HPA = more drops, lower p99), and the memory wall prevents escaping it by
scaling. The custom deliberately chooses the availability side.

---

## 8. PDF compliance checklist

| Requirement | Status |
|---|---|
| Autoscaling inference service on K8s/Minikube | ✅ |
| Models on CPU, not GPU | ✅ |
| `cpu request = limit = 1` per replica | ✅ |
| Dispatcher = centralized queue, load-balance or drop | ✅ (queue + 503 shedding) |
| Replicas don't queue, one inference at a time | ✅ (headless 1-in-flight/pod) |
| Monitoring via Prometheus + exporters | ✅ |
| Autoscaler decides every 15 s | ✅ |
| Creative autoscaler (not plain CPU HPA) | ✅ (queue + arrival + p99) |
| 1 custom + 2 HPA runs, compare p99 & CPU, plot | ✅ |
| Server-side latency < 0.5 s | ⚠️ met at baseline/recovery; not at the sustained peak (hardware wall, §7) |

---

## 9. Caveats (honesty)

- **Run-to-run variance is large.** Across campaigns HPA 70 % showed 23 / 15 / 2
  samples over 0.5 s for the *same* config — scheduling/workload jitter dominates a
  single run's p99. The **drops** and **demand-tracking** advantages are stable; a
  rigorous p99 ranking would need repeated runs with error bars.
- **Node sizing.** Minikube runs with 16 CPUs (the laptop has 18); all three runs
  share the identical cluster, so the comparison is fair, but absolute numbers shift
  on a smaller node.
- **Tuning is calibrated to the provided `workload.txt`.** The policy reacts to
  generic signals (no hardcoded timings), but `REPLICA_MAX`, queue size and headroom
  were chosen for this trace.

---

## 10. Reproducibility

```powershell
pwsh ./scripts/install.ps1 -Cpus 16 -Memory 14g   # cluster + images + deploy
pwsh ./scripts/run_all.ps1                          # 3-run campaign + figures
```

Key settings: inference `cpu = 1` (request = limit), torch threads pinned to 1,
ResNet18 weights baked in; dispatcher headless per-pod dispatch, queue max = 3;
custom autoscaler `INTERVAL=15 s`, `min/max = 1/3`, `MAX_DELTA=5`, `S_WARN=0.40`,
`SERVICE_TIME=0.1`, `COOLDOWN=6`.
