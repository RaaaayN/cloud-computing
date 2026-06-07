# Load tester — Load generation

The load tester generates HTTP traffic to the dispatcher, measures **end-to-end latency**, exports a CSV, and exposes Prometheus metrics.

**Origin:** `load-tester` branch (Sakshi's contribution), integrated and adapted for `POST /submit` + base64 payloads.

**Source files:**
- [`src/load_tester/run.py`](../src/load_tester/run.py) — CLI and load loop
- [`src/load_tester/images.py`](../src/load_tester/images.py) — image download and encoding

---

## Architecture

```mermaid
flowchart LR
  subgraph loadtester [Load Tester Process]
    CLI[CLI args] --> RUN[run loop]
    RUN --> SEND[send async]
    SEND -->|httpx POST /submit| D[Dispatcher]
    RUN --> CSV[results.csv]
    RUN --> MET[/metrics :8003]
  end
  PROM[Prometheus] -->|scrape 15s| MET
```

---

## Load profile (triangle)

The function `target_rps(t, duration, base, peak)` produces a **triangle** profile:

- `t = 0` → RPS = `base`
- `t = duration / 2` → RPS = `peak`
- `t = duration` → RPS = `base`

Linear interpolation between points. ±20% jitter on inter-request interval (`random.uniform(0.8, 1.2)`).

**Example:** `--duration 300 --base 1 --peak 20` → ramp 1→20 req/s over 150 s, then 20→1 over 150 s.

> **Phase 2:** `workload.txt` bursty trace support (course brief) — not implemented yet; the triangle profile validates integration and autoscaler behavior.

---

## Test images

At startup, `fetch_samples()`:

1. Creates the `samples/` directory (local).
2. Downloads 5 ImageNet JPEGs from GitHub if missing.
3. Returns a list of **base64** strings ready for `/submit`.

Samples: `n02085620_60.JPEG`, `n02123045_50.JPEG`, etc.

---

## CLI

```bash
python src/load_tester/run.py --target <URL> [options]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--target` | *(required)* | Dispatcher base URL (e.g. `http://127.0.0.1:8002`) |
| `--duration` | `300` | Total duration in seconds |
| `--base` | `1.0` | Minimum RPS (triangle endpoints) |
| `--peak` | `20.0` | Maximum RPS (triangle peak) |
| `--out` | `results.csv` | Output CSV file |
| `--metrics-port` | `8003` | HTTP port for `/metrics` |

**Environment variable:** `LOADTESTER_METRICS_PORT` (default 8003).

---

## CSV format

```csv
timestamp,status,latency_seconds
1717776000.123,200,0.4521
1717776000.456,-1,15.0000
```

| Column | Description |
|--------|-------------|
| `timestamp` | Unix time (s) at response |
| `status` | HTTP code (`200`, etc.) or `-1` on network error |
| `latency_seconds` | Client E2E duration (submit → response) |

---

## Prometheus metrics

Exposed at `http://0.0.0.0:8003/metrics` for the entire test run.

| Metric | Type | Labels |
|--------|------|--------|
| `loadtester_requests_total` | Counter | `status` (HTTP code or `error`) |
| `loadtester_request_duration_seconds` | Histogram | buckets 0.05–5.0 s |

**Client p99 (PromQL):**
```promql
histogram_quantile(
  0.99,
  sum(rate(loadtester_request_duration_seconds_bucket[1m])) by (le)
)
```

---

## Examples

### Local (60 s, moderate load)

```bash
python src/load_tester/run.py \
  --target http://127.0.0.1:8002 \
  --duration 60 \
  --base 2 \
  --peak 10 \
  --out benchmarks/run_local.csv
```

### Kubernetes (one-shot Job)

```bash
kubectl apply -f k8s/loadtester-job.yaml
kubectl logs -n inference-system job/loadtester -f
```

Manifest: [`k8s/loadtester-job.yaml`](../k8s/loadtester-job.yaml)

- Target: `http://dispatcher.inference-system.svc.cluster.local:8002`
- Duration 300 s, base 1, peak 20
- ClusterIP Service `:8003` for Prometheus scrape

### Docker image

```bash
docker build -f docker/Dockerfile.loadtester -t loadtester:latest .
docker run --rm -p 8003:8003 loadtester:latest \
  --target http://host.docker.internal:8002 \
  --duration 30 --base 1 --peak 5
```

---

## Tests

```bash
python -m pytest tests/test_load_tester.py -v
```

Covers: `target_rps` at endpoints, payload structure, mocked httpx `/submit`, error handling.

---

## Changes from original (`sakshi-load_tester.py`)

| Aspect | Original | Integrated version |
|--------|----------|-------------------|
| Endpoint | `POST /predict` (multipart) | `POST /submit` (JSON base64) |
| Target | Inference directly | Dispatcher |
| Metrics | CSV only | CSV + Prometheus |
| Location | Repo root | `src/load_tester/` |

`sakshi-load_tester.py` was removed after migration (merge `load-tester` → `elastic-autoscaler`).
