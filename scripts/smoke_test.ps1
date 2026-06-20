<#
.SYNOPSIS
  End-to-end smoke test for the deployed stack (in the spirit of run_all.ps1).

  Checks, with PASS/FAIL per item (exits non-zero on any failure):
    1. inference + dispatcher + prometheus are rolled out.
    2. A real ImageNet image sent through POST /submit comes back with labels
       (proves load tester contract -> dispatcher queue -> inference -> ResNet18).
    3. Prometheus has the inference and dispatcher targets UP.
    4. Prometheus is scraping the inference application metrics.

  Use this after scripts\install.ps1 and before experiments\run_all.ps1.

.USAGE
  pwsh ./scripts/smoke_test.ps1
#>
param(
  [string]$Namespace = "inference-system"
)

$ErrorActionPreference = "Stop"
$ns = $Namespace
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$script:failures = 0
function Check($name, [bool]$ok, $detail = "") {
  if ($ok) { Write-Host "  [PASS] $name $detail" -ForegroundColor Green }
  else     { Write-Host "  [FAIL] $name $detail" -ForegroundColor Red; $script:failures++ }
}

function Query-PromScalar([string]$promql) {
  $uri = "http://localhost:9090/api/v1/query?query=$([uri]::EscapeDataString($promql))"
  $resp = Invoke-RestMethod -Uri $uri -TimeoutSec 10
  if ($resp.data.result.Count -ge 1) { return [double]$resp.data.result[0].value[1] }
  return [double]::NaN
}

Write-Host "==> Ensuring base stack is up..." -ForegroundColor Cyan
foreach ($d in "inference","dispatcher","prometheus") {
  kubectl -n $ns rollout status deploy/$d --timeout=180s | Out-Null
}

Write-Host "==> Starting port-forwards (dispatcher 8002, prometheus 9090)..." -ForegroundColor Cyan
$pfDisp = Start-Process -FilePath "kubectl" -ArgumentList "-n",$ns,"port-forward","svc/dispatcher","8002:8002" -PassThru -WindowStyle Hidden
$pfProm = Start-Process -FilePath "kubectl" -ArgumentList "-n",$ns,"port-forward","svc/prometheus","9090:9090" -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 5

try {
  Write-Host "==> Checks:" -ForegroundColor Cyan

  # --- 2. one real inference through the dispatcher ---
  $sample = Get-ChildItem "src/load_tester/samples" -Filter *.JPEG | Select-Object -First 1
  if (-not $sample) {
    Check "bundled sample image present" $false "(no JPEG in src/load_tester/samples)"
  } else {
    $b64  = [Convert]::ToBase64String([IO.File]::ReadAllBytes($sample.FullName))
    $body = @{ data = $b64 } | ConvertTo-Json
    $labels = $null
    try {
      $labels = Invoke-RestMethod -Uri "http://localhost:8002/submit" -Method Post `
                  -Body $body -ContentType "application/json" -TimeoutSec 30
    } catch {
      Check "dispatcher POST /submit succeeds" $false $_.Exception.Message
    }
    if ($labels) {
      Check "dispatcher POST /submit returns labels" ($labels.Count -ge 1) "(top: $($labels[0]))"
      Write-Host "        $($sample.Name) -> $($labels -join ', ')" -ForegroundColor DarkGray
    }
  }

  # --- 3. prometheus targets UP ---
  foreach ($job in "inference","dispatcher") {
    $up = Query-PromScalar "up{job=`"$job`"}"
    Check "prometheus target '$job' is UP" ($up -eq 1)
  }

  # --- 4. inference application metrics scraped ---
  $hasMetric = Query-PromScalar "inference_requests_total"
  Check "prometheus scrapes inference_requests_total" (-not [double]::IsNaN($hasMetric))
}
finally {
  Stop-Process -Id $pfDisp.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $pfProm.Id -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:failures -eq 0) {
  Write-Host "SMOKE TEST PASSED" -ForegroundColor Green
  exit 0
} else {
  Write-Host "SMOKE TEST FAILED ($script:failures check(s))" -ForegroundColor Red
  exit 1
}
