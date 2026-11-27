# Runs the bare-metal side on this Windows machine:
#   gpu-exporter (nvidia-smi -> OTLP to host agent :4327, debug /metrics on :9835)
#   port-forward to the in-cluster OTel gateway (localhost:4317)
#   otelcol-obs host agent (hostmetrics + GPU scrape -> OTLP -> gateway)
# Ctrl+C stops all three.
$ErrorActionPreference = "Continue"  # native tools log to stderr; we check $LASTEXITCODE instead
$root = Split-Path $PSScriptRoot -Parent
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ";$env:USERPROFILE\go\bin"

$bin = "$root\bin"
New-Item -ItemType Directory -Force $bin | Out-Null

if (-not (Test-Path "$bin\gpu-exporter.exe")) {
    Write-Host "==> building gpu-exporter"
    Push-Location "$root\gpu-exporter"; go build -o "$bin\gpu-exporter.exe" .; Pop-Location
}
$collector = "$root\collector\_build\otelcol-obs.exe"
if (-not (Test-Path $collector)) {
    Write-Host "==> building custom collector for Windows with OCB (first time takes a while)"
    go install go.opentelemetry.io/collector/cmd/builder@v0.166.0
    Push-Location "$root\collector"; builder --config builder-config.yaml; Pop-Location
}

$procs = @()
try {
    Write-Host "==> gpu-exporter on 127.0.0.1:9835"
    $procs += Start-Process "$bin\gpu-exporter.exe" -ArgumentList "-listen", "127.0.0.1:9835", "-otlp", "127.0.0.1:4327" -PassThru -NoNewWindow

    Write-Host "==> port-forward obs-otel-gateway 4317 -> localhost:4317"
    $procs += Start-Process kubectl -ArgumentList "-n", "observability", "port-forward", "svc/obs-otel-gateway", "4317:4317" -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 3

    Write-Host "==> host agent (Ctrl+C to stop)"
    $env:GATEWAY_ENDPOINT = "127.0.0.1:4317"
    & $collector --config "$root\collector\configs\host-agent-windows.yaml"
}
finally {
    foreach ($p in $procs) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
}
