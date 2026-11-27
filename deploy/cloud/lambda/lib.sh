# Shared helpers for the Lambda scripts (sourced, not executed).
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
NS=observability

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

ch_password() {
  kubectl -n "$NS" get secret obs-clickhouse -o jsonpath='{.data.CLICKHOUSE_PASSWORD}' | base64 -d
}

# ch "SQL" [extra clickhouse-client args...]  -> runs a query in the ClickHouse pod
ch() {
  local q="$1"; shift
  kubectl -n "$NS" exec -i obs-clickhouse-0 -- clickhouse-client --user otel --password "$(ch_password)" "$@" -q "$q"
}

# event PHASE STATE [extra JSON attributes, each prefixed with a comma]
# Sends an OTLP log record to the node's agent, so test phases show up in
# ClickHouse (otel_logs, ServiceName='loadtest') and as dashboard annotations.
event() {
  local phase="$1" state="$2" extra="${3:-}"
  local ip; ip=$(kubectl -n "$NS" get svc obs-otel-agent -o jsonpath='{.spec.clusterIP}')
  curl -s -o /dev/null --max-time 5 -X POST "http://$ip:4318/v1/logs" \
    -H 'Content-Type: application/json' -d @- <<EOF || true
{"resourceLogs":[{"resource":{"attributes":[
  {"key":"service.name","value":{"stringValue":"loadtest"}},
  {"key":"host.name","value":{"stringValue":"$(hostname)"}}]},
 "scopeLogs":[{"scope":{"name":"loadtests.sh"},"logRecords":[{
  "timeUnixNano":"$(date +%s%N)","severityText":"INFO","severityNumber":9,
  "body":{"stringValue":"$phase $state"},
  "attributes":[{"key":"test.phase","value":{"stringValue":"$phase"}},
                {"key":"test.state","value":{"stringValue":"$state"}}$extra]}]}]}]}
EOF
}

attr() { printf ',{"key":"%s","value":{"stringValue":"%s"}}' "$1" "$2"; }
