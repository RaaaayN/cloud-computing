<#
.SYNOPSIS
  Run the 3 autoscaling experiments back-to-back and plot the comparison.

  Run 1: custom autoscaler (real scaling, --dry-run removed)
  Run 2: HPA at 70% CPU
  Run 3: HPA at 90% CPU

  Only one scaler is active at a time. For each run it resets inference to 1
  replica, starts experiments/collect.py in the background, applies the
  loadtester Job (which replays workload.txt), waits for the Job to complete,
  then stops the collector. Finally it produces comparison_p99.png / _cpu.png.

.USAGE
  pip install -r experiments/requirements.txt   # once
  pwsh ./scripts/run_all.ps1                     # from anywhere in the repo
#>
param(
  [string]$Namespace    = "inference-system",
  [int]   $JobTimeoutSec = 1200,   # max wait per loadtester Job
  [int]   $SettleSec     = 25      # cool-down between runs
)

$ErrorActionPreference = "Stop"
$ns = $Namespace

# Run from the repo root so all relative paths (k8s/, experiments/) resolve
# regardless of where the script is invoked from.
Set-Location (Split-Path -Parent $PSScriptRoot)

function Assert-BaseStack {
  # The runs are meaningless unless inference + dispatcher + prometheus are all up.
  Write-Host "Ensuring base stack (inference, dispatcher, prometheus) is up..." -ForegroundColor Yellow
  foreach ($d in "inference","dispatcher","prometheus") {
    kubectl -n $ns scale deploy/$d --replicas=1 | Out-Null
  }
  foreach ($d in "inference","dispatcher","prometheus") {
    kubectl -n $ns rollout status deploy/$d --timeout=180s | Out-Null
  }
}

function Wait-Settle {
  kubectl -n $ns scale deploy/inference --replicas=1 | Out-Null
  kubectl -n $ns rollout status deploy/inference --timeout=180s | Out-Null
  Write-Host "    settling ${SettleSec}s..." -ForegroundColor DarkGray
  Start-Sleep -Seconds $SettleSec
}

function Invoke-Run([string]$Name, [string]$OutCsv) {
  Write-Host ">>> RUN '$Name' -> $OutCsv" -ForegroundColor Cyan
  kubectl -n $ns delete job loadtester --ignore-not-found | Out-Null
  Wait-Settle

  # background metric collector (auto-reads http://localhost:9090 via port-forward)
  $col = Start-Process -FilePath "python" `
           -ArgumentList "experiments/collect.py","--out",$OutCsv `
           -PassThru -WindowStyle Hidden

  kubectl apply -f k8s/loadtester-job.yaml | Out-Null
  Write-Host "    waiting for loadtester Job to complete (timeout ${JobTimeoutSec}s)..."
  kubectl -n $ns wait --for=condition=complete job/loadtester --timeout="${JobTimeoutSec}s"

  Stop-Process -Id $col.Id -Force -ErrorAction SilentlyContinue
  kubectl -n $ns delete job loadtester --ignore-not-found | Out-Null

  # sanity: warn if no traffic/metrics were captured (all p99 = nan)
  $rows = Import-Csv $OutCsv
  $valid = ($rows | Where-Object { $_.p99_latency -ne 'nan' -and $_.p99_latency }).Count
  if ($valid -eq 0) {
    Write-Host "!!! WARNING: '$Name' captured 0 valid p99 samples - no traffic reached inference. Check dispatcher/prometheus and the loadtester logs." -ForegroundColor Red
  } else {
    Write-Host "    '$Name': $valid rows with p99 data" -ForegroundColor DarkGray
  }
  Write-Host "<<< '$Name' done ($OutCsv)`n" -ForegroundColor Green
}

# --- pre-flight: clean stale collectors / old CSVs ---
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*collect.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item .\custom.csv,.\hpa70.csv,.\hpa90.csv -Force -ErrorAction SilentlyContinue

# --- make sure the whole base stack is running (not just inference) ---
Assert-BaseStack

# --- Prometheus port-forward for collect.py ---
Write-Host "Starting Prometheus port-forward 9090:9090..." -ForegroundColor Yellow
$pf = Start-Process -FilePath "kubectl" `
        -ArgumentList "-n",$ns,"port-forward","svc/prometheus","9090:9090" `
        -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 4

try {
  # ===== RUN 1: custom autoscaler (real scaling) =====
  kubectl -n $ns delete hpa --all --ignore-not-found | Out-Null
  kubectl apply -f k8s/autoscaler-deployment.yaml | Out-Null
  # remove --dry-run so it actually patches replicas
  $patch = '[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":[]}]'
  kubectl -n $ns patch deploy custom-autoscaler --type=json -p $patch | Out-Null
  kubectl -n $ns rollout status deploy/custom-autoscaler --timeout=120s | Out-Null
  Invoke-Run "custom" "custom.csv"

  # ===== RUN 2: HPA 70% =====
  kubectl -n $ns delete deploy custom-autoscaler --ignore-not-found | Out-Null
  kubectl apply -f k8s/hpa-70.yaml | Out-Null
  Invoke-Run "hpa70" "hpa70.csv"

  # ===== RUN 3: HPA 90% =====
  kubectl -n $ns delete hpa inference-hpa --ignore-not-found | Out-Null
  kubectl apply -f k8s/hpa-90.yaml | Out-Null
  Invoke-Run "hpa90" "hpa90.csv"
}
finally {
  kubectl -n $ns delete hpa inference-hpa --ignore-not-found | Out-Null
  Stop-Process -Id $pf.Id -Force -ErrorAction SilentlyContinue
}

# --- figures ---
Write-Host "Generating comparison figures..." -ForegroundColor Yellow
python experiments/plot.py custom.csv hpa70.csv hpa90.csv --out-prefix comparison
Write-Host "All done: comparison_p99.png, comparison_cpu.png" -ForegroundColor Green
