<#
.SYNOPSIS
  One-shot installer for the elastic ML inference stack on Minikube.

  Steps:
    1. Start Minikube (if not already running) and enable metrics-server.
    2. Build the 4 service images inside Minikube's Docker daemon.
    3. Apply every manifest in order and wait for each rollout to be Ready.

  After this, run scripts\smoke_test.ps1 to verify the chain, then
  experiments\run_all.ps1 for the custom-vs-HPA experiment.

.USAGE
  pwsh ./scripts/install.ps1
  pwsh ./scripts/install.ps1 -Cpus 4 -Memory 6g
  pwsh ./scripts/install.ps1 -SkipStart -SkipBuild   # just re-apply manifests
#>
param(
  [string]$Namespace = "inference-system",
  [int]   $Cpus      = 4,
  [string]$Memory    = "6g",
  [string]$Driver    = "docker",
  [switch]$SkipStart,
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$ns = $Namespace
$repoRoot = Split-Path -Parent $PSScriptRoot   # scripts/ -> repo root
Set-Location $repoRoot

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- 1. cluster ---
if (-not $SkipStart) {
  $running = $false
  try { $running = ((& minikube status --format '{{.Host}}' 2>$null) -eq "Running") } catch { $running = $false }
  if ($running) {
    Write-Host "    Minikube already running." -ForegroundColor DarkGray
  } else {
    Step "Starting Minikube (cpus=$Cpus memory=$Memory driver=$Driver)..."
    minikube start --cpus=$Cpus --memory=$Memory --driver=$Driver
  }
  Step "Enabling metrics-server addon (required for the HPA)..."
  minikube addons enable metrics-server | Out-Null
}

Step "Cluster nodes:"
kubectl get nodes

# --- 2. build images inside Minikube's Docker daemon ---
if (-not $SkipBuild) {
  Step "Pointing Docker at Minikube's daemon (docker-env)..."
  & minikube docker-env --shell powershell | Invoke-Expression
  $env:DOCKER_BUILDKIT = "0"   # legacy builder -> plain single-arch image the kubelet can use

  $images = @(
    @{ Tag = "inference:latest";  File = "docker/Dockerfile.inference"  },
    @{ Tag = "dispatcher:latest"; File = "docker/Dockerfile.dispatcher" },
    @{ Tag = "autoscaler:latest"; File = "docker/Dockerfile.autoscaler" },
    @{ Tag = "loadtester:latest"; File = "docker/Dockerfile.loadtester" }
  )
  foreach ($img in $images) {
    Step "Building $($img.Tag) (the inference image pulls CPU torch, first build is slow)..."
    docker build -t $img.Tag -f $img.File .
  }
  Step "Images in the cluster:"
  minikube image ls | Select-String -Pattern "inference|dispatcher|autoscaler|loadtester"
} else {
  Write-Host "    -SkipBuild set: using existing images." -ForegroundColor DarkGray
}

# --- 3. apply manifests in order, waiting for Ready at each step ---
Step "Applying namespace..."
kubectl apply -f k8s/namespace.yaml | Out-Null

Step "Deploying inference..."
kubectl apply -f k8s/inference-deployment.yaml | Out-Null
kubectl -n $ns rollout status deploy/inference --timeout=300s

Step "Deploying dispatcher..."
kubectl apply -f k8s/dispatcher-deployment.yaml | Out-Null
kubectl -n $ns rollout status deploy/dispatcher --timeout=180s

Step "Deploying prometheus..."
kubectl apply -f k8s/prometheus/ | Out-Null
kubectl -n $ns rollout status deploy/prometheus --timeout=180s

Step "All pods:"
kubectl -n $ns get pods

Write-Host ""
Write-Host "Install complete. Next steps:" -ForegroundColor Green
Write-Host "  pwsh ./scripts/smoke_test.ps1     # verify the chain end-to-end"
Write-Host "  pwsh ./experiments/run_all.ps1    # run the custom-vs-HPA experiment"
