# Cross-compiles the Go binaries on this machine (GOOS=linux) and packages them
# into small alpine images, then loads them into minikube.
# Avoids pulling the large golang base image; see Dockerfile (multi-stage) for CI.
param([string]$Cluster = "obs")
$ErrorActionPreference = "Continue"  # native tools log to stderr; we check $LASTEXITCODE instead
$root = Split-Path $PSScriptRoot -Parent
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ";$env:USERPROFILE\go\bin"

function Invoke-LinuxBuild([string]$dir, [string]$out, [string]$pkg) {
    Push-Location $dir
    try {
        $env:GOOS = "linux"; $env:GOARCH = "amd64"; $env:CGO_ENABLED = "0"
        go build -trimpath -ldflags="-s -w" -o $out $pkg
        if ($LASTEXITCODE -ne 0) { throw "go build failed in $dir" }
    } finally {
        Remove-Item Env:GOOS, Env:GOARCH, Env:CGO_ENABLED -ErrorAction SilentlyContinue
        Pop-Location
    }
}

Write-Host "==> services: linux binary"
Invoke-LinuxBuild "$root\services" "bin/demo-linux" "./cmd/demo"

Write-Host "==> collector: OCB generate + linux binary"
& "$root\collector\build-local.ps1" -Os linux

# Build straight into minikube's Docker daemon. (`minikube image load` shells out to
# `wmic`, which recent Windows 11 builds no longer ship.)
Write-Host "==> docker images (inside minikube '$Cluster')"
$saved = @{}
foreach ($v in 'DOCKER_TLS_VERIFY', 'DOCKER_HOST', 'DOCKER_CERT_PATH', 'MINIKUBE_ACTIVE_DOCKERD') { $saved[$v] = [Environment]::GetEnvironmentVariable($v) }
try {
    minikube -p $Cluster docker-env --shell powershell | Invoke-Expression
    docker build -f "$root\services\Dockerfile.local" -t obsdemo/services:0.1.0 "$root\services"
    if ($LASTEXITCODE -ne 0) { throw "services image build failed" }
    docker build -f "$root\collector\Dockerfile.local" -t obsdemo/otelcol-obs:0.1.0 "$root\collector"
    if ($LASTEXITCODE -ne 0) { throw "collector image build failed" }
    docker images obsdemo/* --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'
} finally {
    foreach ($v in $saved.Keys) { [Environment]::SetEnvironmentVariable($v, $saved[$v]) }
}
