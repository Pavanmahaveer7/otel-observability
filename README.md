# otel-observability

End-to-end observability lab: **Go microservices + Kubernetes (minikube, Helm) + bare-metal CPU/GPU**
→ **custom OpenTelemetry Collector** (built with OCB) → **ClickHouse** as the single columnar store for
all three pillars (metrics, logs, traces) → **Grafana** dashboards, with **Prometheus** for PromQL.

```
 Windows laptop (bare metal)                     minikube (namespace: observability / demo)
 ┌──────────────────────────────┐               ┌─────────────────────────────────────────────────────────┐
 │ gpu-exporter (nvidia-smi)    │──OTLP──┐      │  demo: loadgen → nginx → gateway → orders → payments      │
 │ otelcol-obs host agent       │◄───────┘      │                     │ OTLP (node-local Service)          │
 │   hostmetrics (CPU/mem/disk) │               │                     ▼                                    │
 └──────────────┬───────────────┘               │  otel-agent DaemonSet  (otlp, kubeletstats, hostmetrics, │
                │ OTLP via port-forward :4317   │                         filelog → k8sattributes)         │
                └──────────────────────────────►│                     │ OTLP                               │
                                                │                     ▼                                    │
                                                │  otel-gateway  (otlp, k8s_cluster, k8sobjects events,    │
                                                │                 spanmetrics connector, filter, batch)    │
                                                │        │ clickhouse exporter          │ otlphttp (OTLP)  │
                                                │        ▼                              ▼                  │
                                                │   ClickHouse (otel db)           Prometheus              │
                                                │   otel_traces / otel_logs /           │                  │
                                                │   otel_metrics_*                      │                  │
                                                │        └──────────► Grafana ◄─────────┘                  │
                                                └─────────────────────────────────────────────────────────┘
```

## What's in the repo

| Path | What |
|---|---|
| `deploy/helm/obs-stack/files/nginx.conf` | nginx edge proxy: OTel tracing module, JSON access log with trace IDs, stub_status. |
| `services/` | Go module: one binary, 4 roles (`gateway`, `orders`, `payments`, `loadgen`). OTel SDK for traces, metrics (incl. Go runtime) and logs (slog bridge). Errors and slow calls are injected so dashboards have something to show. |
| `gpu-exporter/` | Go: polls `nvidia-smi`, pushes DCGM-named GPU metrics over OTLP; `/metrics` Prometheus text for debugging. |
| `collector/builder-config.yaml` | OCB manifest for our custom collector distribution `otelcol-obs` (v0.166.0). |
| `collector/configs/host-agent-windows.yaml` | Collector config for the bare-metal role. |
| `deploy/helm/obs-stack/` | Umbrella Helm chart: ClickHouse, collector agent + gateway (+ RBAC), demo services, Grafana and Prometheus subcharts, dashboards. |
| `deploy/helm/obs-stack/files/otel-*.yaml` | Collector configs for the agent and gateway roles. |
| `deploy/helm/obs-stack/files/dashboards/` | Grafana dashboards (ClickHouse SQL + PromQL). |
| `scripts/` | `up.ps1`, `build-images.ps1`, `host-agent.ps1`, `ui.ps1`, `down.ps1`, `edge-demo.sh` (nginx scenarios). |

## The collector: three parts per pipeline

| Role | Receivers | Processors | Exporters / connectors |
|---|---|---|---|
| **agent** (DaemonSet) | `otlp`, `kubelet_stats`, `host_metrics`, `file_log` (parses nginx JSON access logs) | `memory_limiter`, `k8s_attributes`, `transform` (nginx severity), `resource`, `batch` | `otlp_grpc` → gateway |
| **gateway** (Deployment) | `otlp`, `k8s_cluster`, `k8s_objects` (events), `nginx` (stub_status) | `memory_limiter`, `filter` (drop health checks), `resource`, `batch` | `clickhouse`, `otlphttp` → Prometheus native OTLP receiver, `spanmetrics` connector (traces → RED metrics) |
| **host agent** (Windows) | `hostmetrics`, `otlp` (from gpu-exporter) | `memory_limiter`, `resource`, `batch` | `otlp` → gateway |

## Quick start (Windows, PowerShell)

Prereqs: Docker Desktop, minikube, kubectl, Helm, Go 1.27+, NVIDIA driver (for `nvidia-smi`).

```powershell
.\scripts\up.ps1          # minikube + images + helm install (release "obs", namespace "observability")
.\scripts\host-agent.ps1  # in a second terminal: laptop CPU + GPU -> gateway
.\scripts\ui.ps1          # in a third terminal: port-forwards
```

- Grafana: http://localhost:3000 (admin / admin) → folder **Observability**
- Prometheus: http://localhost:9090
- ClickHouse SQL console: http://localhost:8123/play (otel / otel-demo-password)

Tear down: `.\scripts\down.ps1` (add `-DeleteCluster` to remove the minikube profile).

## Data model in ClickHouse

The `clickhouse` exporter creates the schema in database `otel` on start (TTL 72h):

| Table | Holds |
|---|---|
| `otel_traces` | spans (`ServiceName`, `SpanName`, `Duration` ns, `StatusCode`, attributes maps) |
| `otel_logs` | logs from apps (OTLP), container logs (filelog), k8s events (k8sobjects) |
| `otel_metrics_gauge` / `_sum` / `_histogram` / `_exponential_histogram` / `_summary` | all metrics: GPU, host, kubelet, cluster, app, spanmetrics |

Example queries:

```sql
-- p95 latency per service, last 15 min
SELECT ServiceName, quantile(0.95)(Duration)/1e6 AS p95_ms, count() AS spans
FROM otel.otel_traces WHERE Timestamp > now() - INTERVAL 15 MINUTE AND SpanKind = 'Server'
GROUP BY ServiceName ORDER BY p95_ms DESC;

-- GPU utilization from the laptop
SELECT toStartOfMinute(TimeUnix) t, avg(Value) FROM otel.otel_metrics_gauge
WHERE MetricName = 'DCGM_FI_DEV_GPU_UTIL' GROUP BY t ORDER BY t;

-- Errors in logs correlated to traces
SELECT Timestamp, ServiceName, SeverityText, Body, TraceId FROM otel.otel_logs
WHERE SeverityNumber >= 17 ORDER BY Timestamp DESC LIMIT 50;
```

## eBPF (OpenTelemetry eBPF Instrumentation)

A privileged OBI DaemonSet adds kernel-level pod-to-pod network flows (service map, bytes per flow)
with zero app changes. On minikube (WSL2 kernel) only network flows work; zero-code request tracing needs a
standard Linux kernel (works on the Lambda node). See docs/DECISIONS.md #14.

## Real GPU hardware (Lambda Cloud)

`deploy/cloud/lambda/` runs the same stack on a real GPU node (k3s) with NVIDIA dcgm-exporter and
node_exporter (NVLink, ECC/XID, InfiniBand, NVMe), load tests (gpu-burn, NCCL all-reduce, fio) and an
export/replay path. See [deploy/cloud/lambda/README.md](deploy/cloud/lambda/README.md).

## Design notes

See [docs/DECISIONS.md](docs/DECISIONS.md).
