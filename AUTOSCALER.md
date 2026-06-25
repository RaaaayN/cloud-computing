# Custom Autoscaler

Our autoscaler scales the ResNet inference deployment based on the request queue
length and the P99 latency, rather than CPU usage like the default Kubernetes HPA.
The idea is that the queue tells us how much work is actually waiting, which reacts
faster than CPU under bursty traffic. We compare it against HPA at 70% and 90% CPU
in the README (Steps 6 to 9).

## Architecture

```
client ──POST /query──► dispatcher (5001) ──► Redis queue (inference_queue)
                              │                      │
                         metrics :8000         forward loop
                              │                      ▼
                              │            inference pods "tu-cloud-project"
                              │              Flask :6001 / metrics :8001
                              ▼                      │
                         Prometheus :9090 ◄──scrapes─┘
                              ▲ reads P99 + queue size
                       custom autoscaler ──kubectl scale──► inference deployment
```

| Component | Deployment / Service | App port | Metrics |
|---|---|---|---|
| Dispatcher | `dispatcher` / `dispatcher-service` | 5001 | 8000 |
| Inference (ResNet) | `tu-cloud-project` | 80 → 6001 | 8001 |
| Redis | `redis` / `redis-service` | 6379 | — |
| Prometheus | `prometheus` / `prometheus-service` | 9090 | — |

## How it works

The loop wakes up every 15 seconds, pulls two values from Prometheus, decides on a
replica count and applies it with `kubectl scale`. Replicas are kept between 1 and 10.

The two values it looks at:

- Queue length, from `dispatcher_queue_size`. This is the main signal.
- P99 latency, from `histogram_quantile(0.99, rate(inference_latency_seconds_bucket[1m]))`.
  This is a backup so we still react if requests get slow before the queue grows.

## Scaling rules

The conditions are checked in order and the first one that matches is used. The
result is always capped between 1 and 10.

| Condition | Action |
|---|---|
| Queue > 200 | add 3 replicas |
| Queue > 50 | add 2 replicas |
| Queue > 10, or P99 > 0.4 s | add 1 replica |
| Queue empty and P99 < 0.3 s (and more than 1 replica) | remove 1 replica |
| anything else | leave it as is |

A bigger backlog means a bigger step up, so a sudden spike doesn't take many cycles
to clear. Scaling down is more cautious: the queue has to be empty *and* latency
below 0.3 s, and we only drop one replica at a time. The gap between the 0.4 s
scale-up threshold and the 0.3 s scale-down threshold stops the replica count from
bouncing up and down around a single value. If Prometheus returns nothing, a missing
queue is treated as 0 and a missing latency won't block a scale-down.

## Two versions and their output

There are two scripts that share the exact same scaling logic:

- `dispatcher/autoscaler.py` only scales.
- `dispatcher/autoscaler_logger.py` does the same thing but also writes
  `autoscaler_log.csv`, a summary in `autoscaler_summary.csv`, and the three plots
  `p99_latency_plot.png`, `queue_size_plot.png` and `replica_count_plot.png`.

Use the logger when running the experiments. `compare_autoscalers.py` then draws the
custom-vs-HPA comparison from the saved CSVs.
