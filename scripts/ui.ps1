# Port-forwards the UIs to localhost. Ctrl+C closes them.
#   Grafana     http://localhost:3000   (admin / admin)
#   Prometheus  http://localhost:9090
#   ClickHouse  http://localhost:8123/play  (otel / otel-demo-password)
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
$forwards = @(
    @("observability", "svc/obs-grafana", "3000:80"),
    @("observability", "svc/obs-prometheus-server", "9090:80"),
    @("observability", "svc/obs-clickhouse", "8123:8123"),
    @("edge", "svc/nginx", "8080:8080"),
    @("edge", "svc/nginx", "8443:8443")
)
$procs = foreach ($f in $forwards) {
    Start-Process kubectl -ArgumentList "-n", $f[0], "port-forward", $f[1], $f[2] -PassThru -WindowStyle Hidden
}
Write-Host "Grafana     http://localhost:3000   (admin / admin)"
Write-Host "Prometheus  http://localhost:9090"
Write-Host "ClickHouse  http://localhost:8123/play  (otel / otel-demo-password)"
Write-Host "nginx       http://localhost:8080   https://localhost:8443 (self-signed)"
Write-Host "Ctrl+C to stop port-forwards."
try { while ($true) { Start-Sleep -Seconds 5 } }
finally { $procs | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } }
