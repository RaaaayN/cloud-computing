#!/usr/bin/env python3
"""Campagne de comparaison custom autoscaler vs HPA pour la branche cloud-project.

Trois scénarios sur le même workload (custom, HPA 70 %, HPA 90 %), l'un après l'autre.
Avant chaque run : reset à 1 réplique, flush Redis, settle de 20 s.
La charge est générée localement (via port-forward dispatcher-service:5001).
Métriques collectées toutes les 15 s depuis Prometheus (port-forward 9090).

    python3 run_campaign.py              # workload complet (629 s)
    python3 run_campaign.py --short      # workload court (90 s) pour sanity check

Écrit dispatcher/{custom_autoscaler,hpa70,hpa90}_log.csv et comparison_plot.png.
"""

import argparse
import csv
import math
import pathlib
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

ROOT = pathlib.Path(__file__).resolve().parent
DISP_DIR = ROOT / "dispatcher"
PROM_URL = "http://localhost:9090"
DISPATCHER_URL = "http://localhost:5001/query"
# Chemin image dans le pod inference
IMAGE_PATH = "/app/images/fire_truck.jpeg"
SETTLE = 20
SCALE_INTERVAL = 15

# Workload court pour sanity check : base 5 rps (20 s) -> burst 30 rps (50 s) -> base 5 rps (20 s)
SHORT_WORKLOAD = [5] * 20 + [30] * 50 + [5] * 20


def k(*args, **kw):
    return subprocess.run(["kubectl", *args], cwd=ROOT,
                          capture_output=True, text=True, **kw)


def prom(expr):
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query",
                         params={"query": expr}, timeout=5)
        res = r.json()["data"]["result"]
        return float(res[0]["value"][1]) if res else math.nan
    except Exception:
        return math.nan


def get_replicas():
    out = k("get", "deploy", "tu-cloud-project",
            "-o", "jsonpath={.spec.replicas}").stdout
    return int(out.strip()) if out.strip() else 1


def compute_target(p99, queue, current):
    # Piste C : signal principal = p99 latency (aligne sur SLO 0.5 s)
    # Scale up si p99 dépasse 350 ms, scale down si p99 < 150 ms
    if not math.isnan(p99) and p99 > 0.35:
        return min(current + 1, 10)
    if (math.isnan(p99) or p99 < 0.15) and current > 1:
        return max(current - 1, 1)
    return current


def collect_and_scale(csv_path, stop, do_scale=True):
    """Log métriques toutes les SCALE_INTERVAL s ; si do_scale, applique la logique custom."""
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "P99_Latency", "Queue_Size", "Replica_Count"])
        while not stop.is_set():
            p99 = prom("histogram_quantile(0.99, rate(inference_latency_seconds_bucket[1m]))")
            queue = prom("dispatcher_queue_size")
            current = get_replicas()
            ts = datetime.now().isoformat(timespec="seconds")

            p99_str = f"{p99:.4f}" if not math.isnan(p99) else "N/A"
            q_str = str(int(queue)) if not math.isnan(queue) else "N/A"
            w.writerow([ts, p99_str, q_str, current])
            f.flush()

            if do_scale:
                target = compute_target(p99, queue if not math.isnan(queue) else 0, current)
                if target != current:
                    k("scale", "deploy", "tu-cloud-project", f"--replicas={target}")
                    print(f"  [scale] {current} → {target} répliques")
            print(f"  [{ts}] p99={p99_str}  q={q_str}  reps={current}")
            stop.wait(SCALE_INTERVAL)


def send_one():
    try:
        requests.post(DISPATCHER_URL,
                      json={"image": IMAGE_PATH},
                      timeout=3)
    except Exception:
        pass


def run_load(workload):
    """Envoie le workload (liste de RPS par seconde) via ThreadPoolExecutor."""
    for sec, rps in enumerate(workload, 1):
        with ThreadPoolExecutor(max_workers=max(rps, 1)) as ex:
            for _ in range(rps):
                ex.submit(send_one)
        time.sleep(1)


def prep():
    k("delete", "hpa", "tu-cloud-project", "--ignore-not-found")
    k("scale", "deploy", "tu-cloud-project", "--replicas=1")
    k("rollout", "status", "deploy/tu-cloud-project", "--timeout=120s")
    subprocess.run(["kubectl", "exec", "deployment/redis", "--",
                    "redis-cli", "flushall"], capture_output=True)
    print(f"  Settle {SETTLE} s…")
    time.sleep(SETTLE)


def run_scenario(name, scaler, workload):
    print(f"\n{'='*52}")
    print(f">>> SCÉNARIO : {name}  ({len(workload)} s de charge)")
    print(f"{'='*52}")
    prep()

    csv_path = DISP_DIR / f"{name}_log.csv"
    stop = threading.Event()
    do_scale = (scaler == "custom")

    if scaler.startswith("hpa"):
        pct = scaler[3:]
        k("autoscale", "deploy", "tu-cloud-project",
          f"--cpu-percent={pct}", "--min=1", "--max=10")
        print(f"  HPA configuré à {pct} % CPU")

    t = threading.Thread(target=collect_and_scale,
                         args=(csv_path, stop, do_scale), daemon=True)
    t.start()

    print(f"  Lancement du load test…")
    run_load(workload)
    print(f"  Charge terminée — attente 15 s pour la dernière mesure…")
    time.sleep(15)

    stop.set()
    t.join()
    k("delete", "hpa", "tu-cloud-project", "--ignore-not-found")
    print(f"<<< {name} terminé — log : {csv_path}")


def plot_comparison():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        scenarios = [
            ("custom_autoscaler", "Custom"),
            ("hpa70", "HPA 70%"),
            ("hpa90", "HPA 90%"),
        ]

        fig, axes = plt.subplots(3, 1, figsize=(12, 12))

        summary_rows = []
        for fname, label in scenarios:
            path = DISP_DIR / f"{fname}_log.csv"
            if not path.exists():
                continue
            rows = list(csv.DictReader(open(path)))
            t = [i * SCALE_INTERVAL for i in range(len(rows))]

            def col(key):
                out = []
                for r in rows:
                    try:
                        out.append(float(r[key]))
                    except (ValueError, KeyError):
                        out.append(math.nan)
                return out

            p99 = col("P99_Latency")
            reps = col("Replica_Count")
            q = col("Queue_Size")

            axes[0].plot(t, p99, marker="o", ms=3, label=label)
            axes[1].plot(t, reps, marker="s", ms=3, label=label)
            axes[2].plot(t, q, marker="x", ms=3, label=label)

            valid_p = [v for v in p99 if not math.isnan(v)]
            valid_r = [v for v in reps if not math.isnan(v)]
            if valid_p:
                summary_rows.append((label,
                                     round(sum(valid_p)/len(valid_p), 3),
                                     round(max(valid_p), 3),
                                     int(max(valid_r)) if valid_r else "N/A"))

        axes[0].axhline(0.5, color="red", ls="--", alpha=0.5, label="cible 0.5s")
        for ax, title, ylabel in [
            (axes[0], "P99 Latency (s)", "latency (s)"),
            (axes[1], "Number of CPU Cores (1 core = 1 replica)", "CPU cores"),
            (axes[2], "Queue Size", "requests"),
        ]:
            ax.set_title(title); ax.set_ylabel(ylabel)
            ax.legend(); ax.grid(alpha=0.3)
        axes[2].set_xlabel("temps (s)")

        plt.tight_layout()
        out = DISP_DIR / "comparison_plot.png"
        plt.savefig(out, dpi=150)
        print(f"\n[✓] Graphique : {out}")

        print(f"\n{'Autoscaler':<18} {'P99 moy':<12} {'P99 max':<12} {'Reps max'}")
        print("-" * 52)
        for label, avg, mx, rmax in summary_rows:
            print(f"{label:<18} {avg:<12} {mx:<12} {rmax}")

    except Exception as e:
        print(f"[!] Erreur génération graphique : {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--short", action="store_true",
                        help="Workload court (90 s) pour sanity check")
    args = parser.parse_args()

    if args.short:
        workload = SHORT_WORKLOAD
        print(f"[INFO] Mode court : {len(workload)} s de charge par scénario (~{len(workload)*3//60} min total)")
    else:
        wf = DISP_DIR / "test" / "workload.txt"
        workload = list(map(int, wf.read_text().split()))
        print(f"[INFO] Workload complet : {len(workload)} s par scénario (~{len(workload)*3//60} min total)")

    # Libérer les ports avant de démarrer les port-forwards
    subprocess.run("lsof -ti:9090 | xargs kill -9 2>/dev/null; true", shell=True)
    subprocess.run("lsof -ti:5001 | xargs kill -9 2>/dev/null; true", shell=True)
    time.sleep(2)

    # Port-forwards
    pf_prom = subprocess.Popen(
        ["kubectl", "port-forward", "svc/prometheus-service", "9090:9090"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    pf_disp = subprocess.Popen(
        ["kubectl", "port-forward", "svc/dispatcher-service", "5001:5001"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    time.sleep(4)

    # Vérification rapide
    try:
        r = requests.post(DISPATCHER_URL,
                          json={"image": IMAGE_PATH}, timeout=5)
        print(f"[✓] Dispatcher répond : {r.status_code} {r.json()}")
    except Exception as e:
        print(f"[!] Dispatcher inaccessible : {e}")
        pf_prom.terminate(); pf_disp.terminate()
        sys.exit(1)

    try:
        run_scenario("custom_autoscaler", "custom", workload)
        run_scenario("hpa70", "hpa70", workload)
        run_scenario("hpa90", "hpa90", workload)
    finally:
        pf_prom.terminate()
        pf_disp.terminate()

    plot_comparison()
    print("\n[✓] Campagne terminée.")


if __name__ == "__main__":
    main()
