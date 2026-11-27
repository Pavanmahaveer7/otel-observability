# Decisions

## 1. ClickHouse is the store for all three pillars
Chosen over "ClickHouse for logs/traces + Prometheus for metrics" and GreptimeDB.
One SQL engine for joins across signals (e.g. trace → logs by `TraceId`, GPU load vs. latency),
columnar compression, TTL per table. Prometheus is kept as a secondary metrics path (the gateway pushes OTLP to its
native receiver) for PromQL and alerting, not as the system of record.

## 2. Custom collector built with OCB, one binary, three roles
`collector/builder-config.yaml` pins every component to the same release (v0.166.0;
`k8sattributesprocessor` is a stable module on its own `v1.x` line, so it is v1.5.0).
The same `otelcol-obs` binary runs as node agent, gateway and Windows host agent; only the config differs.

## 3. Components deliberately left out of the build
- `prometheusreceiver`, `prometheusexporter` and `resourcedetectionprocessor` pull in AWS/GCP/Azure/OCI SDKs
  (hundreds of MB of modules). On this network those large module downloads stall, and we
  don't need cloud discovery/detection on minikube or a laptop.
  - GPU metrics: `gpu-exporter` pushes OTLP instead of being scraped.
  - Prometheus: v3 ingests OTLP natively (`--web.enable-otlp-receiver`); the gateway pushes with the core `otlphttp` exporter. Collector self-metrics are still scraped on :8888.
  - Host/node identity: set with the `resource` processor from env vars (`K8S_NODE_NAME`, `COMPUTERNAME`).
- If you need them later, add them back to `builder-config.yaml`; nothing else depends on their absence.

## 4. Building without `go mod tidy`
OCB's default flow runs `go mod tidy`, which downloads the entire module graph including
test-only dependencies of every component. `collector/build-local.ps1` generates sources with
`--skip-get-modules`, `go get`s the components and runs `go build -mod=mod`, which only fetches
modules the binary imports. `collector/Dockerfile` keeps the standard multi-stage OCB build for CI.

## 5. Images are cross-compiled on the host
`scripts/build-images.ps1` builds linux/amd64 binaries locally and packages them onto `alpine:3.20`
(`Dockerfile.local`) to avoid pulling the large `golang` image. Multi-stage `Dockerfile`s remain for CI.

## 6. Apps → agent via a node-local Service
The agent Service uses `internalTrafficPolicy: Local` so pods always reach the agent on their node,
without relying on hostPort/CNI portmap support. Pods also set `k8s.pod.uid` in
`OTEL_RESOURCE_ATTRIBUTES` (downward API) so `k8sattributes` can always associate them.

## 7. GPU metrics use DCGM names
`DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_FB_USED`, … so the same dashboards work if a Linux GPU node
runs NVIDIA's `dcgm-exporter` later.

## 8. Bare-metal agent reaches the gateway through `kubectl port-forward`
With the minikube docker driver on Windows the node IP isn't routable from the host, so
`scripts/host-agent.ps1` port-forwards the gateway to `localhost:4317`. The gateway Service is also a
NodePort (30317) for drivers where the node IP is reachable (Hyper-V, Linux).

## 9. Fixed release name and namespace
Subchart values (Grafana datasources, Prometheus scrape targets) can't be templated, so the chart
is installed as release `obs` in namespace `observability`.

## 10. Demo-only secrets
ClickHouse and Grafana passwords are in `values.yaml` for a local lab. Override with `--set` or
external Secrets for anything shared.

## 11. Cloud GPU run reuses the same chart
`values-lambda.yaml` turns on what only makes sense on a real GPU node: dcgm-exporter, node_exporter,
the agent's `prometheus/node` receiver (needs the full collector, `builder-config.cloud.yaml`, built on
the node where large module downloads aren't a problem) and ClusterIP-only services reached over SSH
tunnels. The GPU-node dashboard is pure ClickHouse SQL (counter rates computed with window functions)
so an exported run can be replayed locally from Parquet into `lambda_run` and viewed with the same dashboard.

## 12. nginx edge proxy
`loadgen → nginx (edge ns) → gateway`. nginx emits all three signals without extra agents:
- **traces**: official `ngx_otel_module` (`nginx:*-otel` image), `otel_trace_context propagate` continues the
  caller's trace and passes it to gateway, so traces run edge → payments unbroken. The exporter endpoint must be
  the fully qualified `...svc.cluster.local` name: the module's gRPC resolver ignores DNS search domains.
- **logs**: JSON access log on stdout with `$otel_trace_id`/`$otel_span_id`; the agent's `file_log` parses it into
  attributes and trace/span IDs (logs ↔ traces linking). Severity comes from HTTP status via a `transform` rule.
- **metrics**: `stub_status` read by the gateway's `nginx` receiver in its own `metrics/nginx` pipeline.
The edge 5xx rate includes the demo's injected payment declines (gateway answers 502 on a failed checkout).

## 13. Edge features on nginx (and the passive-health-check lesson)
`files/nginx.conf` + `files/nginx-locations.conf`, all knobs in `values.yaml` under `nginx:`:
- **Load balancing per pod**: headless `gateway-headless` Service + `server ... resolve` + `least_conn`, so
  `upstream_addr` shows individual pods (a ClusterIP would hide them behind kube-proxy).
- **Canary**: `gateway-v2` (same binary, `SERVICE_VERSION=0.2.0`, +40 ms, 3% 500s) gets
  `nginx.canary.weight`% of `/api/*` via `split_clients`; `route` in the access log is the pool.
- **Timeout** 500 ms (slow payments → 504), **retries** only on connection error/503 (never on timeout),
  **rate limit** per client (429), **edge cache** for `GET /api/catalog` (10 s, stale-while-updating),
  **path routing** (`/static/` served by nginx), **TLS** on 8443 with a Helm-generated self-signed cert.
- **Lesson:** with nginx's default `max_fails=1`, single slow requests (timeouts) ejected healthy pods for
  10 s each; with all three ejected nginx answered `no live upstreams` → 502 for everyone. Fixed with
  `max_fails=5 fail_timeout=10s` (`nginx.passiveHealth`). Real-world cascade, visible on the Edge dashboard.
- Bursts are generated from inside the cluster (`scripts/edge-demo.sh`): `kubectl port-forward` collapses
  under ~100 parallel connections.

## 14. eBPF with OpenTelemetry eBPF Instrumentation (OBI v1.0.0)
`templates/obi.yaml`, values under `ebpf:`. Privileged DaemonSet (`hostPID`, `hostNetwork`), config v1 YAML via
`OTEL_EBPF_CONFIG_PATH`, OTLP/HTTP to the node agent, k8s metadata on. Instruments `demo`, `edge`,
`observability` but never the collectors (feedback loop).
- **Network flows work on minikube** (`obi.network.flow.bytes` with src/dst namespace + owner): requires
  `hostNetwork: true` and `OTEL_EBPF_NETWORK_SOURCE=tc` — the default socket filter only saw OBI's own traffic
  on Docker Desktop's WSL2 network. Dashboard: *eBPF - network flows*.
- **Zero-code request tracing does not work on minikube/WSL2**: OBI's process tracer needs a kprobe on
  `security_socket_accept`, which doesn't exist because Microsoft's WSL2 kernel (5.15) is built with
  `# CONFIG_SECURITY_NETWORK is not set`. It works on standard distro kernels (the Lambda node), where the same
  chart enables it automatically.
- The host's BPF filesystem is mounted (`/sys/fs/bpf`, bidirectional) so OBI can pin maps.
