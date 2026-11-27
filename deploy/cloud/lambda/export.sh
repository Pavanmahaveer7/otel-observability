#!/usr/bin/env bash
# Dumps all ClickHouse telemetry to Parquet plus results/ into one tarball, so the
# run survives terminating the instance. Then copy it to your laptop:
#   scp ubuntu@<instance-ip>:~/obs-run-*.tar.gz .
# and load it locally with scripts/import-run.ps1.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/obs-run-$STAMP"
mkdir -p "$OUT/parquet"

log "Exporting ClickHouse tables to Parquet"
for t in $(ch "SELECT name FROM system.tables WHERE database='otel' AND engine LIKE '%MergeTree%' ORDER BY name"); do
  ch "SELECT * FROM otel.$t FORMAT Parquet" > "$OUT/parquet/$t.parquet"
  printf '  %-40s %s\n' "$t" "$(du -h "$OUT/parquet/$t.parquet" | cut -f1)"
done

log "Adding results/ and cluster state"
cp -r "$ROOT/results" "$OUT/" 2>/dev/null || true
kubectl get pods -A -o wide > "$OUT/pods.txt"
helm -n observability get values obs -a > "$OUT/helm-values.yaml"

tar -czf "$OUT.tar.gz" -C "$HOME" "$(basename "$OUT")"
log "Wrote $OUT.tar.gz ($(du -h "$OUT.tar.gz" | cut -f1))"
echo "On your laptop:  scp ubuntu@<instance-ip>:$OUT.tar.gz ."
echo "Then terminate the instance in the Lambda console to stop billing."
