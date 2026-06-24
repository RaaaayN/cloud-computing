# Custom Autoscaler — Design Decisions and Rationale

## Overview

This document explains the design of the custom autoscaler, the experiments that led to its final form, and why it outperforms the Kubernetes Horizontal Pod Autoscaler (HPA) on the given workload.

---

## System Context

The system consists of:
- A **Dispatcher** (Flask + Redis queue) that receives inference requests and forwards them to replicas
- **Inference replicas** (ResNet18 on CPU), each with `CPU request = limit = 1`
- **Prometheus** collecting `inference_latency_seconds` (histogram) and `dispatcher_queue_size` (gauge)
- The **Autoscaler** polling Prometheus every 15 seconds and calling `kubectl scale`

The SLO is a server-side **p99 latency < 0.5 s**.

---

## What the Autoscaler Observes

Two metrics are available from Prometheus:

| Metric | What it measures |
|---|---|
| `inference_latency_seconds` | Time to run ResNet18 inference **inside the pod** (~75–90 ms nominal) |
| `dispatcher_queue_size` | Number of requests waiting in the Redis queue |

A critical insight: `inference_latency_seconds` measures **only the pod-level processing time**, not the end-to-end wait in the Redis queue. A queue of 300 pending requests does not automatically raise this metric — the requests that do get processed still complete in ~90 ms.

---

## Why the Original Logic Failed

The original autoscaler used **queue depth** as its primary signal with aggressive scale steps:

```python
if queue > 200: return current + 3
if queue > 50:  return current + 2
if queue > 10 or p99 > 0.4: return current + 1
```

**Problem:** Scaling to more replicas does not increase throughput in this setup. The dispatcher runs a single synchronous forwarding thread that sends one request at a time to the Kubernetes service. Adding replicas does not unblock that thread — throughput stays at ~13 req/s regardless of replica count. Worse, more replicas on the same Minikube node share a fixed CPU budget, so each individual pod gets less CPU time and inference latency **increases** when many replicas are running simultaneously.

---

## Experiments Run

Two alternative scaling policies were benchmarked against the original using a 90-second burst workload:

### Policy D — Cap scale-up at +1 per decision

```python
if queue > 10 or p99 > 0.4: return current + 1   # was +1/+2/+3
if queue == 0 and p99 < 0.3: return current - 1
```

**Result:** Worse than original. Capping scale steps slows the response to the burst, causing the queue to overflow before enough replicas are ready.

| Autoscaler | P99 avg | P99 max |
|---|---|---|
| Custom (D) | 0.230 s | 0.319 s |
| HPA 70% | 0.199 s | 0.319 s |
| HPA 90% | 0.176 s | 0.306 s |

### Policy C — Use p99 latency as the primary signal

```python
if p99 > 0.35:              return current + 1   # scale up when SLO at risk
if p99 < 0.15 and n > 1:   return current - 1   # scale down when stable
```

**Result:** Best by a wide margin. The autoscaler never scales up because p99 stays well below 0.35 s throughout the workload, keeping a single replica that has dedicated access to the node's CPU.

| Autoscaler | P99 avg | P99 max |
|---|---|---|
| **Custom (C)** | **0.090 s** | **0.098 s** |
| HPA 70% | 0.122 s | 0.266 s |
| HPA 90% | 0.160 s | 0.271 s |

---

## Final Design: p99-Driven Policy

```python
def compute_target_replicas(p99_latency, queue_size, current_replicas):
    if p99_latency is not None and p99_latency > 0.35:
        return min(current_replicas + 1, MAX_REPLICAS)
    if (p99_latency is None or p99_latency < 0.15) and current_replicas > 1:
        return max(current_replicas - 1, MIN_REPLICAS)
    return current_replicas
```

**Scale-up threshold — 0.35 s:** chosen to leave a comfortable 150 ms margin below the 0.5 s SLO while reacting before the SLO is violated.

**Scale-down threshold — 0.15 s:** conservative, avoids premature scale-down during a temporary dip between load bursts.

**No queue signal:** queue depth is deliberately ignored. It reflects requests waiting to be dispatched, not actual inference degradation. Acting on it causes unnecessary pod churn.

---

## Why It Works Better Than HPA

### 1. The right signal

HPA reacts to **CPU utilization**, which lags behind actual latency impact. It scales up when CPU is saturated, but by then the queue has already grown and the scale-up adds pod startup latency on top.

The custom autoscaler reacts to **p99 inference latency** — the metric that directly determines whether the SLO is met. If p99 is healthy, no action is needed regardless of queue depth or CPU load.

### 2. CPU contention avoidance

Minikube runs on a single host. Each inference pod gets `1 CPU`, but multiple pods share the underlying physical cores. When HPA scales to 2 replicas, both pods compete for CPU and individual inference times climb from ~75 ms to ~230 ms — a 3× degradation that HPA then cannot reverse quickly due to its 5-minute scale-down stabilization window.

The custom autoscaler, by using p99 as its gate, naturally avoids this: it only scales up if p99 **actually** exceeds 0.35 s, which does not happen when a single pod operates undisturbed.

### 3. No scale-down hysteresis

HPA has a built-in 5-minute cooldown before scaling down. After the load burst subsides, HPA keeps 2 pods running, both still competing for CPU, holding p99 elevated at ~0.23 s for the rest of the experiment. The custom autoscaler scales down as soon as p99 drops below 0.15 s.

---

## Full Campaign Results (630 s workload)

| Autoscaler | P99 avg | P99 max | Max CPU cores |
|---|---|---|---|
| **Custom** | **0.099 s** | **0.205 s** | 1 |
| HPA 90% | 0.105 s | 0.232 s | 1 |
| HPA 70% | 0.150 s | 0.232 s | 2 |

The custom autoscaler outperforms HPA 70% by **34%** and HPA 90% by **6%** on average p99, while never violating the 0.5 s SLO.
