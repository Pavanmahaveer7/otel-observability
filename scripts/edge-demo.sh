#!/usr/bin/env bash
# Edge (nginx) scenarios. Runs requests from inside the cluster (the nginx pod),
# so kubectl port-forward isn't in the path. Watch Grafana -> "Edge - nginx".
#
#   ./scripts/edge-demo.sh burst      # rate limiting -> 429
#   ./scripts/edge-demo.sh cache      # MISS -> HIT on a fresh key
#   ./scripts/edge-demo.sh failover   # delete a gateway pod under traffic
#   ./scripts/edge-demo.sh canary 50  # send 50% of checkouts to gateway-v2 (helm upgrade)
#   ./scripts/edge-demo.sh tls        # request over HTTPS
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EDGE="http://nginx.edge.svc:8080"
in_nginx() { kubectl exec -n edge deploy/nginx -- sh -c "$1"; }

case "${1:-}" in
  burst)
    echo "150 parallel requests from one client (limit 20 r/s, burst 20):"
    in_nginx "curl -s -Z --parallel-max 100 -o /dev/null -w '%{http_code}\n' '$EDGE/api/checkout?burst=[1-150]' | sort | uniq -c" ;;
  cache)
    k=$RANDOM
    in_nginx "for i in 1 2 3; do curl -s -o /dev/null -D - -w 'time=%{time_total}s\n' '$EDGE/api/catalog?k=$k' | grep -E 'X-Cache-Status|time=' | tr '\r\n' '  '; echo; done" ;;
  failover)
    in_nginx "for i in \$(seq 80); do curl -s -o /dev/null -w '%{http_code}\n' '$EDGE/api/checkout?fo=\$i'; sleep 0.1; done" > /tmp/edge-fo.txt &
    sleep 1
    victim=$(kubectl get pods -n demo -l app.kubernetes.io/name=gateway -o jsonpath='{.items[0].metadata.name}')
    echo "deleting $victim under traffic"; kubectl delete pod -n demo "$victim" --wait=false >/dev/null
    wait; sort /tmp/edge-fo.txt | uniq -c ;;
  canary)
    w="${2:?weight 0-100}"
    helm upgrade obs "$ROOT/deploy/helm/obs-stack" -n observability --reuse-values --set nginx.canary.weight="$w" >/dev/null
    kubectl -n edge rollout status deploy/nginx
    echo "canary weight now $w%" ;;
  tls)
    in_nginx "curl -sk -o /dev/null -w 'HTTPS %{http_code} via %{scheme}\n' https://nginx.edge.svc:8443/api/catalog" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
