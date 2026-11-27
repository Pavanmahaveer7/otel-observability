# Brings up the whole k8s side: minikube, images, Helm release.
#   .\scripts\up.ps1            # full
#   .\scripts\up.ps1 -SkipBuild # reuse images already loaded
param(
    [string]$Cluster = "obs",
    [switch]$SkipBuild
)
$ErrorActionPreference = "Continue"  # native tools log to stderr; we check $LASTEXITCODE instead
$root = Split-Path $PSScriptRoot -Parent
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

Step "minikube ($Cluster)"
$status = minikube status -p $Cluster --format '{{.Host}}' 2>$null
if ($status -ne "Running") {
    minikube start -p $Cluster --driver=docker --cpus=8 --memory=12288 --disk-size=40g --addons=metrics-server
}
kubectl config use-context $Cluster | Out-Null

if (-not $SkipBuild) {
    Step "build + load images (services, OCB custom collector)"
    & "$PSScriptRoot\build-images.ps1" -Cluster $Cluster
}

Step "helm install obs-stack"
$chart = "$root\deploy\helm\obs-stack"
helm repo add grafana https://grafana.github.io/helm-charts 2>$null | Out-Null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>$null | Out-Null
helm dependency build $chart | Out-Null
# Release name "obs" and namespace "observability" are referenced by subchart values.
helm upgrade --install obs $chart -n observability --create-namespace --wait --timeout 10m
# Restart collectors so config changes are always picked up even when the image tag is unchanged.
kubectl -n observability rollout restart deploy/obs-otel-gateway ds/obs-otel-agent | Out-Null

Step "status"
kubectl get pods -n observability
kubectl get pods -n demo
Write-Host ""
Write-Host "Next:" -ForegroundColor Green
Write-Host "  .\scripts\ui.ps1          # Grafana http://localhost:3000 (admin/admin), Prometheus :9090, ClickHouse :8123"
Write-Host "  .\scripts\host-agent.ps1  # bare-metal agent: this laptop's CPU + NVIDIA GPU -> gateway"
