#!/usr/bin/env bash
# Binds the UIs to 127.0.0.1 on the node (nothing is exposed publicly).
# Reach them from your laptop through an SSH tunnel:
#   ssh -N -L 3000:localhost:3000 -L 9090:localhost:9090 -L 8123:localhost:8123 ubuntu@<instance-ip>
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
pids=()
trap 'kill "${pids[@]}" 2>/dev/null' EXIT INT TERM
kubectl -n observability port-forward --address 127.0.0.1 svc/obs-grafana 3000:80 >/dev/null & pids+=($!)
kubectl -n observability port-forward --address 127.0.0.1 svc/obs-prometheus-server 9090:80 >/dev/null & pids+=($!)
kubectl -n observability port-forward --address 127.0.0.1 svc/obs-clickhouse 8123:8123 >/dev/null & pids+=($!)
echo "Grafana    http://localhost:3000       (admin / admin)"
echo "Prometheus http://localhost:9090"
echo "ClickHouse http://localhost:8123/play  (otel / see values.yaml)"
wait
