#!/usr/bin/env bash
# Checks what is actually arriving in ClickHouse from this node.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

log "Pods"
kubectl get pods -A --no-headers | awk '{printf "  %-15s %-48s %s\n", $1, $2, $4}'

log "Rows per table"
ch "SELECT table, sum(rows) AS rows FROM system.parts WHERE database='otel' AND active GROUP BY table ORDER BY table FORMAT PrettyCompactMonoBlock"

log "Signal families (last 10 min)"
ch "
SELECT family, count() AS points, uniqExact(MetricName) AS metrics FROM (
  SELECT MetricName, multiIf(
    MetricName LIKE 'DCGM_FI_PROF_NVLINK%', 'GPU NVLink (DCGM)',
    MetricName LIKE 'DCGM_FI_PROF_%',       'GPU profiling (DCGM)',
    MetricName LIKE 'DCGM_FI_%',            'GPU device (DCGM)',
    MetricName LIKE 'node_infiniband%',     'InfiniBand (node_exporter)',
    MetricName LIKE 'node_disk%' OR MetricName LIKE 'node_nvme%', 'Disk / NVMe (node_exporter)',
    MetricName LIKE 'node_%',               'Other node_exporter',
    MetricName LIKE 'k8s.%' OR MetricName LIKE 'container.%', 'Kubernetes',
    MetricName LIKE 'system.%',             'Host (hostmetrics)',
    'App / runtime / span metrics') AS family
  FROM merge('otel', '^otel_metrics_(gauge|sum|histogram)$')
  WHERE TimeUnix > now() - INTERVAL 10 MINUTE)
GROUP BY family ORDER BY family FORMAT PrettyCompactMonoBlock"

log "Latest DCGM values per GPU"
ch "
SELECT Attributes['gpu'] AS gpu, any(Attributes['modelName']) AS model,
  round(argMaxIf(Value, TimeUnix, MetricName='DCGM_FI_DEV_GPU_UTIL'), 1)        AS util_pct,
  round(argMaxIf(Value, TimeUnix, MetricName='DCGM_FI_DEV_POWER_USAGE'), 1)     AS power_w,
  round(argMaxIf(Value, TimeUnix, MetricName='DCGM_FI_DEV_GPU_TEMP'), 1)        AS temp_c,
  argMaxIf(Value, TimeUnix, MetricName='DCGM_FI_DEV_XID_ERRORS')                AS last_xid
FROM otel.otel_metrics_gauge
WHERE MetricName LIKE 'DCGM_FI_DEV_%' AND TimeUnix > now() - INTERVAL 5 MINUTE
GROUP BY gpu ORDER BY toUInt32OrZero(gpu) FORMAT PrettyCompactMonoBlock"

log "Collector errors (last 200 lines)"
for w in ds/obs-otel-agent deploy/obs-otel-gateway; do
  n=$(kubectl -n "$NS" logs "$w" --tail=200 2>/dev/null | grep -c $'\terror\t' || true)
  echo "  $w: $n error lines"
done
