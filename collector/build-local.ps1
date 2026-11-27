# Builds the OCB distribution without `go mod tidy`.
# OCB's default flow runs `go mod tidy`, which downloads the *whole* module graph
# (incl. test-only deps of every component, e.g. multi-hundred-MB cloud SDKs).
# Here we generate sources only, `go get` each component, and let `go build`
# fetch just the modules the binary actually imports.
#
#   .\collector\build-local.ps1               # Windows binary  -> _build\otelcol-obs.exe
#   .\collector\build-local.ps1 -Os linux     # Linux binary    -> _build\otelcol-obs-linux
param([ValidateSet("windows", "linux")][string]$Os = "windows")
$ErrorActionPreference = "Continue"  # native tools log to stderr; we check $LASTEXITCODE instead
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ";$env:USERPROFILE\go\bin"
Set-Location $PSScriptRoot

if (-not (Get-Command builder -ErrorAction SilentlyContinue)) {
    go install go.opentelemetry.io/collector/cmd/builder@v0.166.0
}
if (-not (Test-Path "_build\go.mod")) {
    builder --config builder-config.yaml --skip-compilation --skip-get-modules
    # Collect "module version" pairs from the manifest.
    $mods = Select-String -Path builder-config.yaml -Pattern '^\s*-\s*gomod:\s*(\S+)\s+(v\S+)' |
        ForEach-Object { "$($_.Matches[0].Groups[1].Value)@$($_.Matches[0].Groups[2].Value)" }
    Push-Location _build
    go mod init otelcol-obs
    go get @mods go.opentelemetry.io/collector/otelcol@v0.166.0
    Pop-Location
}

Push-Location _build
try {
    $out = "otelcol-obs.exe"
    if ($Os -eq "linux") { $env:GOOS = "linux"; $env:GOARCH = "amd64"; $out = "otelcol-obs-linux" }
    $env:CGO_ENABLED = "0"
    go build -mod=mod -trimpath -ldflags="-s -w" -o $out .
    if ($LASTEXITCODE -ne 0) { throw "go build failed" }
    Write-Host "built _build\$out"
} finally {
    Remove-Item Env:GOOS, Env:GOARCH, Env:CGO_ENABLED -ErrorAction SilentlyContinue
    Pop-Location
}
