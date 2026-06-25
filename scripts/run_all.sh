#!/usr/bin/env bash
set -euo pipefail

export PYTHONIOENCODING=utf-8

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DISPATCHER="$ROOT/dispatcher"
TEST_DIR="$DISPATCHER/test"
LOG="$ROOT/scripts/run_all.log"

exec > >(tee -a "$LOG") 2>&1

run_scenario() {
  local name=$1
  local log_only=$2
  local hpa_pct=${3:-}

  echo ""
  echo "========== Scenario: $name $(date -Iseconds) =========="
  kubectl delete hpa tu-cloud-project 2>/dev/null || true
  kubectl scale deployment tu-cloud-project --replicas=1
  sleep 5

  if [ -n "$hpa_pct" ]; then
    kubectl autoscale deployment tu-cloud-project --cpu-percent="$hpa_pct" --min=1 --max=10
    kubectl get hpa
  fi

  kubectl exec deployment/redis -- redis-cli flushall
  echo "Timestamp,P99_Latency,Queue_Size,Replica_Count" > "$DISPATCHER/autoscaler_log.csv"

  cd "$DISPATCHER"
  if [ "$log_only" = "1" ]; then
    AUTOSCALER_LOG_ONLY=1 python autoscaler_logger.py &
  else
    python autoscaler_logger.py &
  fi
  local as_pid=$!
  cd "$ROOT"

  echo "Waiting 30s for metrics to stabilize..."
  sleep 30

  cd "$TEST_DIR"
  python test.py
  cd "$ROOT"

  kill "$as_pid" 2>/dev/null || true
  wait "$as_pid" 2>/dev/null || true

  cp "$DISPATCHER/autoscaler_log.csv" "$DISPATCHER/${name}.csv"
  kubectl delete hpa tu-cloud-project 2>/dev/null || true
  echo "Saved $DISPATCHER/${name}.csv ($(wc -l < "$DISPATCHER/${name}.csv") lines)"
}

echo "=== Full experiment started $(date -Iseconds) ==="
kubectl get pods

run_scenario custom_autoscaler_log 0 ""
run_scenario hpa70_log 1 70
run_scenario hpa90_log 1 90

cd "$DISPATCHER"
python compare_autoscalers.py custom_autoscaler_log.csv hpa70_log.csv hpa90_log.csv
python analyze_autoscaler_log.py

echo "=== All scenarios complete $(date -Iseconds) ==="
ls -la "$DISPATCHER"/*.csv "$DISPATCHER"/*.png 2>/dev/null || true
