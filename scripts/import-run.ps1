# Loads an exported cloud run (obs-run-*.tar.gz from deploy/cloud/lambda/export.sh)
# into the local minikube ClickHouse, database `lambda_run` (no TTL, kept until dropped).
# The "GPU node" dashboard has a Database selector to switch between otel and lambda_run.
#   .\scripts\import-run.ps1 -Archive .\obs-run-20261125T101500Z.tar.gz
param([Parameter(Mandatory)][string]$Archive, [string]$Database = "lambda_run")
$ErrorActionPreference = "Continue"
$tar = Join-Path $env:SystemRoot 'System32\tar.exe'   # Windows' built-in bsdtar (not always on PATH)
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')

$work = Join-Path $env:TEMP ("obs-import-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force $work | Out-Null
& $tar -xzf $Archive -C $work
$dir = Get-ChildItem $work -Directory | Select-Object -First 1
$parquetDir = Join-Path $dir.FullName "parquet"
if (-not (Test-Path $parquetDir)) { Write-Error "no parquet/ folder in $Archive"; exit 1 }

$pod = "obs-clickhouse-0"
$pw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(
        (kubectl -n observability get secret obs-clickhouse -o jsonpath='{.data.CLICKHOUSE_PASSWORD}')))
function Invoke-CH([string]$q) {
    kubectl -n observability exec $pod -- clickhouse-client --user otel --password $pw -q $q
}

Invoke-CH "CREATE DATABASE IF NOT EXISTS $Database"
Push-Location $parquetDir   # kubectl cp needs relative paths on Windows (drive letter colon)
try {
    foreach ($f in Get-ChildItem *.parquet) {
        $t = $f.BaseName
        kubectl -n observability cp $f.Name "${pod}:/var/lib/clickhouse/user_files/$($f.Name)" | Out-Null
        Invoke-CH "DROP TABLE IF EXISTS $Database.$t"
        Invoke-CH "CREATE TABLE $Database.$t ENGINE = MergeTree ORDER BY tuple() AS SELECT * FROM file('$($f.Name)', Parquet)"
        $rows = Invoke-CH "SELECT count() FROM $Database.$t"
        kubectl -n observability exec $pod -- rm -f "/var/lib/clickhouse/user_files/$($f.Name)" | Out-Null
        "{0,-40} {1,10} rows" -f "$Database.$t", $rows
    }
} finally { Pop-Location }
Write-Host "Imported into database '$Database'. Results/tool output: $($dir.FullName)\results"
