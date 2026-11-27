# Packs the repo (without build outputs) for upload to a cloud GPU node.
#   .\scripts\package-for-cloud.ps1
#   scp otel-observability.tgz ubuntu@<instance-ip>:~
#   ssh ubuntu@<instance-ip> "tar -xzf otel-observability.tgz && cd otel-observability && ./deploy/cloud/lambda/bootstrap.sh"
$ErrorActionPreference = "Continue"
$tar = Join-Path $env:SystemRoot 'System32\tar.exe'   # Windows' built-in bsdtar (not always on PATH)
$root = Split-Path $PSScriptRoot -Parent
$parent = Split-Path $root -Parent
$name = Split-Path $root -Leaf
$out = Join-Path (Get-Location) "otel-observability.tgz"

# Shell scripts must keep LF endings to run on Linux.
$crlf = Get-ChildItem "$root\deploy\cloud" -Recurse -Filter *.sh | Where-Object { [IO.File]::ReadAllText($_.FullName).Contains("`r") }
if ($crlf) { Write-Error "CRLF line endings in: $($crlf.FullName -join ', ')"; exit 1 }

& $tar -czf $out -C $parent `
    --exclude "$name/.git" --exclude "$name/bin" --exclude "$name/services/bin" `
    --exclude "$name/collector/_build" --exclude "$name/deploy/helm/obs-stack/charts" `
    --exclude "$name/results" --exclude "*.exe" $name
if ($LASTEXITCODE -ne 0) { Write-Error "tar failed"; exit 1 }
"{0} ({1:N1} MB)" -f $out, ((Get-Item $out).Length / 1MB)
Write-Host ""
Write-Host "Next:"
Write-Host "  scp otel-observability.tgz ubuntu@<instance-ip>:~"
Write-Host "  ssh ubuntu@<instance-ip>"
Write-Host "  tar -xzf otel-observability.tgz && cd otel-observability && ./deploy/cloud/lambda/bootstrap.sh"
