"""Generates the Grafana dashboards in deploy/helm/obs-stack/files/dashboards/.

    python tools/gen_dashboards.py

Most panels query ClickHouse (the store for all three pillars) with SQL; a few
use Prometheus (PromQL over the same metrics, pushed by the gateway via OTLP).
"""
import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "deploy/helm/obs-stack/files/dashboards"
CH = {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"}
PROM = {"type": "prometheus", "uid": "prometheus"}

# sqlds format codes used by the ClickHouse plugin
FMT = {"timeseries": 0, "table": 1, "logs": 2, "traces": 3}


def sql(q, kind="timeseries", ref="A"):
    return {"datasource": CH, "editorType": "sql", "format": FMT[kind], "queryType": kind,
            "rawSql": " ".join(q.split()), "refId": ref}


def promql(expr, legend="", ref="A"):
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref}


class Dash:
    def __init__(self, uid, title, tags, variables=(), annotations=()):
        self.uid, self.title, self.tags = uid, title, tags
        self.panels, self.y, self.id = [], 0, 1
        self.variables = list(variables)
        self.annotations = list(annotations)

    def row(self, title):
        self.panels.append({"type": "row", "title": title, "id": self._id(), "collapsed": False,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}})
        self.y += 1
        self._x, self._rowh = 0, 0

    def panel(self, ptype, title, targets, w=12, h=8, unit=None, ds=CH, options=None, field=None,
              description=None):
        if getattr(self, "_x", 0) + w > 24:
            self.y += self._rowh
            self._x, self._rowh = 0, 0
        defaults = {"unit": unit} if unit else {}
        if field:
            defaults.update(field)
        p = {"type": ptype, "title": title, "id": self._id(), "datasource": ds,
             "gridPos": {"h": h, "w": w, "x": self._x, "y": self.y},
             "targets": targets, "fieldConfig": {"defaults": defaults, "overrides": []},
             "options": options or {}}
        if description:
            p["description"] = description
        self.panels.append(p)
        self._x += w
        self._rowh = max(self._rowh, h)
        if self._x >= 24:
            self.y += self._rowh
            self._x, self._rowh = 0, 0

    def _id(self):
        self.id += 1
        return self.id

    def write(self):
        d = {"uid": self.uid, "title": self.title, "tags": self.tags, "schemaVersion": 39,
             "editable": True, "refresh": "30s", "time": {"from": "now-30m", "to": "now"},
             "timepicker": {}, "timezone": "browser", "panels": self.panels,
             "templating": {"list": self.variables}, "annotations": {"list": self.annotations}}
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{self.uid}.json"
        path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        print("wrote", path.relative_to(OUT.parent.parent.parent.parent.parent))


def ch_var(name, label, query, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": CH,
            "query": " ".join(query.split()), "definition": query, "refresh": 2,
            "includeAll": include_all, "multi": True, "allValue": "", "sort": 1,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]} if include_all else {}}


TS = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10, "showPoints": "never"}
LEGEND = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
          "tooltip": {"mode": "multi", "sort": "desc"}}
SERVER = "SpanKind = 'Server' AND ResourceAttributes['service.namespace'] = 'demo'"

# ---------------------------------------------------------------------------- services
d = Dash("obs-services", "Services - RED (traces in ClickHouse)", ["otel", "traces", "clickhouse"])
d.row("Rate, errors, duration (server spans)")
d.panel("timeseries", "Requests / s by service", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time, ServiceName, count() / $__interval_s AS rps
    FROM otel.otel_traces WHERE $__timeFilter(Timestamp) AND {SERVER}
    GROUP BY time, ServiceName ORDER BY time""")], w=8, unit="reqps", options=LEGEND,
    field={"custom": TS})
d.panel("timeseries", "Error % by service", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time, ServiceName,
           countIf(StatusCode = 'Error') * 100 / count() AS error_pct
    FROM otel.otel_traces WHERE $__timeFilter(Timestamp) AND {SERVER}
    GROUP BY time, ServiceName ORDER BY time""")], w=8, unit="percent", options=LEGEND,
    field={"custom": TS})
d.panel("timeseries", "p95 latency by service", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time, ServiceName, quantile(0.95)(Duration) / 1e6 AS p95_ms
    FROM otel.otel_traces WHERE $__timeFilter(Timestamp) AND {SERVER}
    GROUP BY time, ServiceName ORDER BY time""")], w=8, unit="ms", options=LEGEND,
    field={"custom": TS})
d.row("Endpoints and errors")
d.panel("table", "Endpoints (server spans)", [sql(f"""
    SELECT ServiceName AS service, SpanName AS endpoint, count() AS calls,
           round(countIf(StatusCode = 'Error') * 100 / count(), 2) AS error_pct,
           round(quantile(0.50)(Duration) / 1e6, 1) AS p50_ms,
           round(quantile(0.95)(Duration) / 1e6, 1) AS p95_ms,
           round(quantile(0.99)(Duration) / 1e6, 1) AS p99_ms
    FROM otel.otel_traces WHERE $__timeFilter(Timestamp) AND {SERVER}
    GROUP BY service, endpoint ORDER BY p95_ms DESC""", "table")], w=12, h=9)
d.panel("table", "Recent error spans", [sql("""
    SELECT Timestamp AS time, ServiceName AS service, SpanName AS span, TraceId AS trace_id,
           round(Duration / 1e6, 1) AS ms, StatusMessage AS message
    FROM otel.otel_traces WHERE $__timeFilter(Timestamp) AND StatusCode = 'Error'
    ORDER BY Timestamp DESC LIMIT 50""", "table")], w=12, h=9)
d.row("Business metrics (Prometheus, pushed by the gateway over OTLP)")
d.panel("timeseries", "Orders / s by result", [promql(
    "sum by (result) (rate(orders_created_total[1m]))", "{{result}}")], w=12, ds=PROM,
    unit="ops", options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Span-metrics p95 by service (spanmetrics connector)", [promql(
    "histogram_quantile(0.95, sum by (le, service_name) "
    "(rate(traces_span_metrics_duration_milliseconds_bucket{span_kind=\"SPAN_KIND_SERVER\"}[2m])))",
    "{{service_name}}")], w=12, ds=PROM, unit="ms", options=LEGEND, field={"custom": TS})
d.row("Application logs (OTLP)")
d.panel("logs", "Demo service logs", [sql("""
    SELECT Timestamp AS timestamp, Body AS body, SeverityText AS level, ServiceName AS service,
           TraceId AS trace_id
    FROM otel.otel_logs
    WHERE $__timeFilter(Timestamp) AND ResourceAttributes['service.namespace'] = 'demo'
    ORDER BY Timestamp DESC LIMIT 300""", "logs")], w=24, h=10,
    options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
             "enableLogDetails": True})
d.write()

# ---------------------------------------------------------------------------- GPU + host
gpu = "MetricName = '{m}' AND $__timeFilter(TimeUnix)"
d = Dash("obs-gpu-host", "Bare metal - GPU & host (ClickHouse)", ["otel", "gpu", "host", "clickhouse"])
d.row("NVIDIA GPU (gpu-exporter, DCGM names)")
for title, metric, unit, mx in [("GPU util", "DCGM_FI_DEV_GPU_UTIL", "percent", 100),
                                ("Temperature", "DCGM_FI_DEV_GPU_TEMP", "celsius", 100),
                                ("Power", "DCGM_FI_DEV_POWER_USAGE", "watt", None),
                                ("VRAM used", "DCGM_FI_DEV_FB_USED", "decmbytes", None)]:
    field = {"min": 0, **({"max": mx} if mx else {}),
             "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                          {"color": "orange", "value": (mx or 1e9) * 0.75},
                                                          {"color": "red", "value": (mx or 1e9) * 0.9}]}}
    d.panel("gauge" if mx else "stat", title, [sql(f"""
        SELECT argMax(Value, TimeUnix) AS value FROM otel.otel_metrics_gauge
        WHERE {gpu.format(m=metric)}""", "table")], w=6, h=5, unit=unit, field=field)
d.panel("timeseries", "GPU & memory-controller utilization", [sql("""
    SELECT $__timeInterval(TimeUnix) AS time, MetricName AS metric, avg(Value) AS value
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName IN ('DCGM_FI_DEV_GPU_UTIL', 'DCGM_FI_DEV_MEM_COPY_UTIL')
    GROUP BY time, metric ORDER BY time""")], w=12, unit="percent", options=LEGEND,
    field={"custom": TS, "min": 0, "max": 100})
d.panel("timeseries", "VRAM used / total (MiB)", [sql("""
    SELECT $__timeInterval(TimeUnix) AS time, MetricName AS metric, avg(Value) AS value
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName IN ('DCGM_FI_DEV_FB_USED', 'DCGM_FI_DEV_FB_TOTAL')
    GROUP BY time, metric ORDER BY time""")], w=12, unit="decmbytes", options=LEGEND,
    field={"custom": TS, "min": 0})
d.panel("timeseries", "Power (W) and temperature (C)", [sql("""
    SELECT $__timeInterval(TimeUnix) AS time, MetricName AS metric, avg(Value) AS value
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName IN ('DCGM_FI_DEV_POWER_USAGE', 'DCGM_FI_DEV_GPU_TEMP')
    GROUP BY time, metric ORDER BY time""")], w=12, options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Clocks (MHz)", [sql("""
    SELECT $__timeInterval(TimeUnix) AS time, MetricName AS metric, avg(Value) AS value
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName IN ('DCGM_FI_DEV_SM_CLOCK', 'DCGM_FI_DEV_MEM_CLOCK')
    GROUP BY time, metric ORDER BY time""")], w=12, unit="hertz", options=LEGEND,
    field={"custom": TS})
d.row("Hosts: bare-metal laptop + minikube node (hostmetrics)")
d.panel("timeseries", "CPU busy % by host", [sql("""
    SELECT $__timeInterval(TimeUnix) AS time, ResourceAttributes['host.name'] AS host,
           100 * (1 - avgIf(Value, Attributes['state'] = 'idle')) AS busy_pct
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName = 'system.cpu.utilization'
    GROUP BY time, host ORDER BY time""")], w=12, unit="percent", options=LEGEND,
    field={"custom": TS, "min": 0, "max": 100},
    description="1 - average idle share across all logical CPUs.")
d.panel("timeseries", "Memory used % by host", [sql("""
    SELECT $__timeInterval(TimeUnix) AS time, ResourceAttributes['host.name'] AS host,
           100 * avgIf(Value, Attributes['state'] = 'used') AS used_pct
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName = 'system.memory.utilization'
    GROUP BY time, host ORDER BY time""")], w=12, unit="percent", options=LEGEND,
    field={"custom": TS, "min": 0, "max": 100})
d.panel("table", "Filesystem usage (latest)", [sql("""
    SELECT ResourceAttributes['host.name'] AS host, Attributes['mountpoint'] AS mount,
           round(100 * argMax(Value, TimeUnix), 1) AS used_pct
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName = 'system.filesystem.utilization'
    GROUP BY host, mount ORDER BY used_pct DESC""", "table")], w=12, h=8)
d.panel("timeseries", "Network throughput by host (Prometheus)", [promql(
    "sum by (host_name, network_io_direction) (rate(system_network_io_bytes_total[2m]))",
    "{{host_name}} {{network_io_direction}}")], w=12, ds=PROM, unit="Bps", options=LEGEND,
    field={"custom": TS})
d.write()

# ---------------------------------------------------------------------------- kubernetes
ns_var = ch_var("namespace", "Namespace", """
    SELECT DISTINCT ResourceAttributes['k8s.namespace.name'] FROM otel.otel_metrics_gauge
    WHERE MetricName LIKE 'k8s.pod.%' AND TimeUnix > now() - INTERVAL 1 HOUR""")
NS ="ResourceAttributes['k8s.namespace.name'] IN (${namespace:singlequote})"
d = Dash("obs-k8s", "Kubernetes - minikube (ClickHouse)", ["otel", "k8s", "clickhouse"], [ns_var])
d.row("Cluster state (k8s_cluster receiver)")
d.panel("stat", "Nodes ready", [sql("""
    SELECT sum(v) AS value FROM (SELECT ResourceAttributes['k8s.node.name'] AS n, argMax(Value, TimeUnix) AS v
    FROM otel.otel_metrics_gauge WHERE $__timeFilter(TimeUnix) AND MetricName = 'k8s.node.condition_ready'
    GROUP BY n)""", "table")], w=6, h=5)
d.panel("stat", "Pods running", [sql(f"""
    SELECT countIf(v = 2) AS value FROM (SELECT ResourceAttributes['k8s.pod.uid'] AS p, argMax(Value, TimeUnix) AS v
    FROM otel.otel_metrics_gauge WHERE $__timeFilter(TimeUnix) AND MetricName = 'k8s.pod.phase' AND {NS}
    AND TimeUnix > now() - INTERVAL 2 MINUTE GROUP BY p)""", "table")], w=6, h=5)
d.panel("stat", "Pods not running", [sql(f"""
    SELECT countIf(v != 2 AND v != 3) AS value FROM (SELECT ResourceAttributes['k8s.pod.uid'] AS p, argMax(Value, TimeUnix) AS v
    FROM otel.otel_metrics_gauge WHERE $__timeFilter(TimeUnix) AND MetricName = 'k8s.pod.phase' AND {NS}
    AND TimeUnix > now() - INTERVAL 2 MINUTE GROUP BY p)""", "table")], w=6, h=5,
    field={"thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                         {"color": "red", "value": 1}]}})
d.panel("stat", "Container restarts (window)", [sql(f"""
    SELECT sum(d) AS value FROM (SELECT ResourceAttributes['k8s.pod.uid'] AS p, ResourceAttributes['k8s.container.name'] AS c,
    max(Value) - min(Value) AS d FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName = 'k8s.container.restarts' AND {NS} GROUP BY p, c)""", "table")],
    w=6, h=5, field={"thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                                  {"color": "orange", "value": 1}]}})
d.panel("table", "Deployments", [sql(f"""
    SELECT ResourceAttributes['k8s.namespace.name'] AS namespace, ResourceAttributes['k8s.deployment.name'] AS deployment,
           argMaxIf(Value, TimeUnix, MetricName = 'k8s.deployment.desired') AS desired,
           argMaxIf(Value, TimeUnix, MetricName = 'k8s.deployment.available') AS available
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName IN ('k8s.deployment.desired', 'k8s.deployment.available') AND {NS}
    GROUP BY namespace, deployment ORDER BY namespace, deployment""", "table")], w=24, h=7)
d.row("Resource usage (kubeletstats receiver)")
d.panel("timeseries", "Pod CPU (cores)", [sql(f"""
    SELECT $__timeInterval(TimeUnix) AS time, ResourceAttributes['k8s.pod.name'] AS pod, avg(Value) AS cores
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName IN ('k8s.pod.cpu.usage', 'k8s.pod.cpu.utilization') AND {NS}
    GROUP BY time, pod ORDER BY time""")], w=12, h=9, options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Pod memory working set", [sql(f"""
    SELECT $__timeInterval(TimeUnix) AS time, ResourceAttributes['k8s.pod.name'] AS pod, avg(Value) AS bytes
    FROM otel.otel_metrics_gauge
    WHERE $__timeFilter(TimeUnix) AND MetricName = 'k8s.pod.memory.working_set' AND {NS}
    GROUP BY time, pod ORDER BY time""")], w=12, h=9, unit="bytes", options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Node CPU (cores) and memory", [
    sql("""SELECT $__timeInterval(TimeUnix) AS time, 'cpu cores' AS metric, avg(Value) AS value
           FROM otel.otel_metrics_gauge WHERE $__timeFilter(TimeUnix)
           AND MetricName IN ('k8s.node.cpu.usage', 'k8s.node.cpu.utilization') GROUP BY time ORDER BY time"""),
    sql("""SELECT $__timeInterval(TimeUnix) AS time, 'memory GiB' AS metric, avg(Value) / 1073741824 AS value
           FROM otel.otel_metrics_gauge WHERE $__timeFilter(TimeUnix)
           AND MetricName = 'k8s.node.memory.working_set' GROUP BY time ORDER BY time""", ref="B")],
    w=24, h=8, options=LEGEND, field={"custom": TS})
d.row("Events and container logs")
d.panel("table", "Kubernetes events (k8sobjects receiver)", [sql("""
    SELECT Timestamp AS time,
           JSONExtractString(Body, 'object', 'type') AS type,
           JSONExtractString(Body, 'object', 'reason') AS reason,
           JSONExtractString(Body, 'object', 'regarding', 'kind') AS kind,
           JSONExtractString(Body, 'object', 'regarding', 'namespace') AS namespace,
           JSONExtractString(Body, 'object', 'regarding', 'name') AS object,
           JSONExtractString(Body, 'object', 'note') AS note
    FROM otel.otel_logs
    WHERE $__timeFilter(Timestamp) AND LogAttributes['event.domain'] = 'k8s'
    ORDER BY Timestamp DESC LIMIT 200""", "table")], w=24, h=9)
d.panel("logs", "Container logs (filelog receiver)", [sql(f"""
    SELECT Timestamp AS timestamp, Body AS body, SeverityText AS level,
           ResourceAttributes['k8s.pod.name'] AS pod, ResourceAttributes['k8s.container.name'] AS container
    FROM otel.otel_logs
    WHERE $__timeFilter(Timestamp) AND LogAttributes['log.iostream'] != '' AND {NS}
    ORDER BY Timestamp DESC LIMIT 300""", "logs")], w=24, h=10,
    options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True})
d.write()

# ---------------------------------------------------------------------------- pipeline health
d = Dash("obs-pipeline", "Pipeline - collector & ClickHouse health", ["otel", "collector", "clickhouse"])
d.row("Data landing in ClickHouse")
# In ClickHouse a trailing ORDER BY only applies to the last SELECT of a UNION ALL,
# so the union is wrapped in a subquery and sorted outside.
d.panel("timeseries", "Rows / s written per signal", [sql("""
    SELECT time, signal, v FROM (
      SELECT $__timeInterval(Timestamp) AS time, 'traces' AS signal, count() / $__interval_s AS v
        FROM otel.otel_traces WHERE $__timeFilter(Timestamp) GROUP BY time
      UNION ALL
      SELECT $__timeInterval(Timestamp) AS time, 'logs' AS signal, count() / $__interval_s AS v
        FROM otel.otel_logs WHERE $__timeFilter(Timestamp) GROUP BY time
      UNION ALL
      SELECT $__timeInterval(TimeUnix) AS time, 'metrics (gauge+sum+hist)' AS signal, count() / $__interval_s AS v
        FROM merge('otel', '^otel_metrics_(gauge|sum|histogram)$') WHERE $__timeFilter(TimeUnix) GROUP BY time
    ) ORDER BY time""")], w=12, unit="short", options=LEGEND, field={"custom": TS})
d.panel("table", "Table sizes", [sql("""
    SELECT table, sum(rows) AS total_rows, formatReadableSize(sum(data_compressed_bytes)) AS compressed,
           formatReadableSize(sum(data_uncompressed_bytes)) AS uncompressed,
           round(sum(data_uncompressed_bytes) / greatest(sum(data_compressed_bytes), 1), 1) AS ratio
    FROM system.parts WHERE database = 'otel' AND active GROUP BY table ORDER BY total_rows DESC""", "table")],
    w=12, h=8)
d.panel("table", "Sources reporting (last 5 min)", [sql("""
    SELECT ServiceName AS service, ResourceAttributes['host.name'] AS host,
           ResourceAttributes['host.role'] AS role, count() AS points, max(TimeUnix) AS last_seen
    FROM otel.otel_metrics_gauge WHERE TimeUnix > now() - INTERVAL 5 MINUTE
    GROUP BY service, host, role ORDER BY service""", "table")], w=24, h=8)
d.row("Gateway collector (self-telemetry, Prometheus)")
d.panel("timeseries", "Accepted points / s by receiver", [promql(
    "sum by (receiver) (rate(otelcol_receiver_accepted_metric_points[2m]))", "metrics {{receiver}}"),
    promql("sum by (receiver) (rate(otelcol_receiver_accepted_spans[2m]))", "spans {{receiver}}", "B"),
    promql("sum by (receiver) (rate(otelcol_receiver_accepted_log_records[2m]))", "logs {{receiver}}", "C")],
    w=12, ds=PROM, options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Exporter sent vs failed / s", [promql(
    "sum by (exporter) (rate(otelcol_exporter_sent_metric_points[2m]))", "sent metrics {{exporter}}"),
    promql("sum by (exporter) (rate(otelcol_exporter_send_failed_metric_points[2m]))",
           "FAILED metrics {{exporter}}", "B"),
    promql("sum by (exporter) (rate(otelcol_exporter_sent_spans[2m]))", "sent spans {{exporter}}", "C"),
    promql("sum by (exporter) (rate(otelcol_exporter_sent_log_records[2m]))", "sent logs {{exporter}}", "D")],
    w=12, ds=PROM, options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Exporter queue size", [promql(
    "max by (exporter) (otelcol_exporter_queue_size)", "{{exporter}}")], w=12, ds=PROM, options=LEGEND,
    field={"custom": TS})
d.panel("timeseries", "Gateway memory (RSS)", [promql(
    "max(otelcol_process_memory_rss)", "rss")], w=12, ds=PROM, unit="bytes", options=LEGEND,
    field={"custom": TS})
d.write()

# ---------------------------------------------------------------------------- GPU node (cloud)
# Real datacenter GPU node: dcgm-exporter + node_exporter scraped by the agent.
# Pure ClickHouse SQL (rates computed from counters) so it also works on an
# imported run: the Database selector switches between live `otel` and `lambda_run`.
DB = "${database}"
db_var = {"name": "database", "label": "Database", "type": "custom", "query": "otel,lambda_run",
          "current": {"text": "otel", "value": "otel"}, "options": [
              {"text": "otel", "value": "otel", "selected": True},
              {"text": "lambda_run", "value": "lambda_run", "selected": False}]}
phases = {"name": "Load-test phases", "enable": True, "iconColor": "orange", "datasource": CH,
          "target": {"refId": "Anno", "datasource": CH, "editorType": "sql", "format": 1, "queryType": "table",
                     "rawSql": f"SELECT Timestamp AS time, Body AS text FROM {DB}.otel_logs "
                               "WHERE ServiceName = 'loadtest' AND $__timeFilter(Timestamp) ORDER BY time"}}


def names(*base):
    """Counter names with and without the Prometheus `_total` suffix (receiver normalization varies)."""
    return ", ".join(f"'{n}'" for b in base for n in (b, b + "_total"))


def gauge_ts(metrics, series, scale=1, agg="avg"):
    return sql(f"""
        SELECT $__timeInterval(TimeUnix) AS time, {series} AS series, {agg}(Value) * {scale} AS value
        FROM {DB}.otel_metrics_gauge
        WHERE $__timeFilter(TimeUnix) AND MetricName IN ({metrics})
        GROUP BY time, series ORDER BY time""")


def counter_rate(metrics, series, scale=1):
    # Per-series counter -> per-second rate: max per interval, then delta to previous interval.
    return sql(f"""
        SELECT time, series,
               greatest(v - lagInFrame(v, 1, v) OVER (PARTITION BY series ORDER BY time
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0) / $__interval_s * {scale} AS value
        FROM (SELECT $__timeInterval(TimeUnix) AS time, {series} AS series, max(Value) AS v
              FROM {DB}.otel_metrics_sum
              WHERE $__timeFilter(TimeUnix) AND MetricName IN ({metrics})
              GROUP BY time, series)
        ORDER BY time""")


GPU = "concat('GPU ', Attributes['gpu'])"
d = Dash("obs-gpu-node", "Cloud GPU node - DCGM, NVLink, InfiniBand, NVMe (ClickHouse)",
         ["otel", "gpu", "dcgm", "neocloud", "clickhouse"], [db_var], [phases])
d.row("GPU activity (DCGM)")
d.panel("timeseries", "GPU utilization %", [gauge_ts("'DCGM_FI_DEV_GPU_UTIL'", GPU)], w=12,
        unit="percent", options=LEGEND, field={"custom": TS, "min": 0, "max": 100})
d.panel("timeseries", "SM active %", [gauge_ts("'DCGM_FI_PROF_SM_ACTIVE'", GPU, 100)], w=12,
        unit="percent", options=LEGEND, field={"custom": TS, "min": 0, "max": 100},
        description="Fraction of time at least one warp was active on an SM (DCGM profiling).")
d.panel("timeseries", "Tensor core active %", [gauge_ts("'DCGM_FI_PROF_PIPE_TENSOR_ACTIVE'", GPU, 100)], w=12,
        unit="percent", options=LEGEND, field={"custom": TS, "min": 0, "max": 100})
d.panel("timeseries", "HBM / DRAM active %", [gauge_ts("'DCGM_FI_PROF_DRAM_ACTIVE'", GPU, 100)], w=12,
        unit="percent", options=LEGEND, field={"custom": TS, "min": 0, "max": 100})
d.row("Interconnect: NVLink, PCIe, InfiniBand")
d.panel("timeseries", "NVLink bandwidth per GPU (TX + RX)", [gauge_ts(
        "'DCGM_FI_PROF_NVLINK_TX_BYTES', 'DCGM_FI_PROF_NVLINK_RX_BYTES'", GPU, 1, "sum")], w=12,
        unit="Bps", options=LEGEND, field={"custom": TS},
        description="Sum of TX and RX bytes/s per GPU. Lights up during the NCCL all-reduce phase.")
d.panel("timeseries", "PCIe bandwidth per GPU (TX + RX)", [gauge_ts(
        "'DCGM_FI_PROF_PCIE_TX_BYTES', 'DCGM_FI_PROF_PCIE_RX_BYTES'", GPU, 1, "sum")], w=12,
        unit="Bps", options=LEGEND, field={"custom": TS})
d.panel("timeseries", "InfiniBand / RoCE port throughput", [counter_rate(
        names("node_infiniband_port_data_received_bytes", "node_infiniband_port_data_transmitted_bytes"),
        "concat(Attributes['device'], ':', Attributes['port'], ' ', "
        "if(MetricName LIKE '%received%', 'rx', 'tx'))")], w=12, unit="Bps", options=LEGEND,
        field={"custom": TS}, description="Empty if the node has no HCAs (common on single-node instances).")
d.panel("table", "InfiniBand port errors (window)", [sql(f"""
        SELECT Attributes['device'] AS device, Attributes['port'] AS port,
               replaceRegexpOne(MetricName, '^node_infiniband_|_total$', '') AS counter,
               max(Value) - min(Value) AS increase
        FROM {DB}.otel_metrics_sum
        WHERE $__timeFilter(TimeUnix) AND MetricName IN ({names(
            'node_infiniband_link_downed', 'node_infiniband_link_error_recovery',
            'node_infiniband_port_errors_received', 'node_infiniband_symbol_error',
            'node_infiniband_port_transmit_wait')})
        GROUP BY device, port, counter ORDER BY increase DESC""", "table")], w=12, h=8)
d.row("GPU health")
d.panel("table", "XID errors (non-zero)", [sql(f"""
        SELECT Attributes['gpu'] AS gpu, Value AS xid, max(TimeUnix) AS last_seen, count() AS samples
        FROM {DB}.otel_metrics_gauge
        WHERE $__timeFilter(TimeUnix) AND MetricName = 'DCGM_FI_DEV_XID_ERRORS' AND Value > 0
        GROUP BY gpu, xid ORDER BY last_seen DESC""", "table")], w=8, h=8,
        description="Last XID per GPU. 48/63/64/74/79/94/95 are the ones that usually need a ticket.")
d.panel("table", "ECC & row remapping (latest)", [sql(f"""
        SELECT Attributes['gpu'] AS gpu,
               argMaxIf(Value, TimeUnix, MetricName LIKE 'DCGM_FI_DEV_ECC_SBE_VOL_TOTAL%') AS ecc_sbe,
               argMaxIf(Value, TimeUnix, MetricName LIKE 'DCGM_FI_DEV_ECC_DBE_VOL_TOTAL%') AS ecc_dbe,
               argMaxIf(Value, TimeUnix, MetricName LIKE 'DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS%') AS remap_corr,
               argMaxIf(Value, TimeUnix, MetricName LIKE 'DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS%') AS remap_uncorr,
               argMaxIf(Value, TimeUnix, MetricName LIKE 'DCGM_FI_DEV_ROW_REMAP_FAILURE%') AS remap_fail,
               argMaxIf(Value, TimeUnix, MetricName LIKE 'DCGM_FI_DEV_PCIE_REPLAY_COUNTER%') AS pcie_replays
        FROM merge('{DB}', '^otel_metrics_(gauge|sum)$')
        WHERE $__timeFilter(TimeUnix) AND MetricName LIKE 'DCGM_FI_DEV_%'
        GROUP BY gpu ORDER BY toUInt32OrZero(gpu)""", "table")], w=16, h=8)
d.row("Power & thermals")
d.panel("timeseries", "Power per GPU", [gauge_ts("'DCGM_FI_DEV_POWER_USAGE'", GPU)], w=8,
        unit="watt", options=LEGEND, field={"custom": TS})
d.panel("timeseries", "GPU temperature", [gauge_ts("'DCGM_FI_DEV_GPU_TEMP'", GPU)], w=8,
        unit="celsius", options=LEGEND, field={"custom": TS})
d.panel("timeseries", "HBM temperature", [gauge_ts("'DCGM_FI_DEV_MEMORY_TEMP'", GPU)], w=8,
        unit="celsius", options=LEGEND, field={"custom": TS})
d.row("Storage: local NVMe / disks (node_exporter)")
d.panel("timeseries", "Disk throughput", [counter_rate(
        names("node_disk_read_bytes", "node_disk_written_bytes"),
        "concat(Attributes['device'], if(MetricName LIKE '%read%', ' read', ' write'))")], w=12,
        unit="Bps", options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Disk busy % (io_time)", [counter_rate(
        names("node_disk_io_time_seconds"), "Attributes['device']", 100)], w=12,
        unit="percent", options=LEGEND, field={"custom": TS, "min": 0, "max": 100})
d.panel("table", "Filesystems (latest)", [sql(f"""
        SELECT Attributes['mountpoint'] AS mount, Attributes['device'] AS device, Attributes['fstype'] AS fstype,
               round(argMaxIf(Value, TimeUnix, MetricName = 'node_filesystem_size_bytes') / 1e9, 1) AS size_gb,
               round(argMaxIf(Value, TimeUnix, MetricName = 'node_filesystem_avail_bytes') / 1e9, 1) AS avail_gb,
               round(100 * (1 - avail_gb / greatest(size_gb, 0.001)), 1) AS used_pct
        FROM {DB}.otel_metrics_gauge
        WHERE $__timeFilter(TimeUnix) AND MetricName IN ('node_filesystem_size_bytes', 'node_filesystem_avail_bytes')
          AND Attributes['fstype'] NOT IN ('tmpfs', 'overlay', 'squashfs')
        GROUP BY mount, device, fstype ORDER BY size_gb DESC""", "table")], w=24, h=7)
d.row("Load-test timeline")
d.panel("table", "Load-test events (loadtests.sh)", [sql(f"""
        SELECT Timestamp AS time, LogAttributes['test.phase'] AS phase, LogAttributes['test.state'] AS state,
               LogAttributes['nccl.avg_busbw_gbps'] AS nccl_busbw_gbps, LogAttributes['fio.bw_mbps'] AS fio_mbps,
               LogAttributes['fio.iops'] AS fio_iops, LogAttributes['fio.mean_lat_us'] AS fio_lat_us,
               LogAttributes['result'] AS result
        FROM {DB}.otel_logs
        WHERE $__timeFilter(Timestamp) AND ServiceName = 'loadtest'
        ORDER BY Timestamp""", "table")], w=24, h=9)
d.write()
# ---------------------------------------------------------------------------- edge (nginx)
# Access logs (file_log -> JSON-parsed attributes), nginx spans (ngx_otel_module),
# stub_status metrics (nginx receiver). All ClickHouse.
NGX = "FROM otel.otel_logs WHERE $__timeFilter(Timestamp) AND ServiceName = 'nginx' AND LogAttributes['status'] != ''"
RT = "toFloat64OrZero(LogAttributes['request_time'])"
URT = "toFloat64OrZero(LogAttributes['upstream_response_time'])"
d = Dash("obs-edge", "Edge - nginx (logs, traces, stub_status)", ["otel", "nginx", "edge", "clickhouse"])
d.row("Traffic and errors (access logs)")
d.panel("timeseries", "Requests / s by status class", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time, concat(substring(LogAttributes['status'], 1, 1), 'xx') AS class,
           count() / $__interval_s AS rps
    {NGX} GROUP BY time, class ORDER BY time""")], w=12, unit="reqps", options=LEGEND,
    field={"custom": {**TS, "stacking": {"mode": "normal"}, "fillOpacity": 40}})
d.panel("timeseries", "Error % (4xx, 5xx)", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time,
           countIf(LogAttributes['status'] LIKE '4%') * 100 / count() AS `4xx %`,
           countIf(LogAttributes['status'] LIKE '5%') * 100 / count() AS `5xx %`
    {NGX} GROUP BY time ORDER BY time""")], w=12, unit="percent", options=LEGEND, field={"custom": TS})
d.row("Latency: user-facing vs backend")
d.panel("timeseries", "p95 latency: total (request_time) vs upstream", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time,
           quantile(0.95)({RT}) * 1000 AS `total p95`,
           quantile(0.95)({URT}) * 1000 AS `upstream p95`,
           quantile(0.50)({RT}) * 1000 AS `total p50`
    {NGX} GROUP BY time ORDER BY time""")], w=12, unit="ms", options=LEGEND, field={"custom": TS},
    description="request_time = what the client saw; upstream_response_time = time waiting on gateway.")
d.panel("timeseries", "nginx overhead p95 (total - upstream)", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time, quantile(0.95)(greatest({RT} - {URT}, 0)) * 1000 AS overhead_ms
    {NGX} GROUP BY time ORDER BY time""")], w=12, unit="ms", options=LEGEND, field={"custom": TS},
    description="Time spent in nginx itself: queueing, TLS, buffering. Should stay near zero.")
d.row("Connections (stub_status via the nginx receiver)")
# nginx.connections_current is an up-down counter -> stored in otel_metrics_sum (IsMonotonic=false).
d.panel("timeseries", "Connections by state", [sql("""
    SELECT $__timeInterval(TimeUnix) AS time, Attributes['state'] AS state, avg(Value) AS connections
    FROM otel.otel_metrics_sum
    WHERE $__timeFilter(TimeUnix) AND MetricName = 'nginx.connections_current'
    GROUP BY time, state ORDER BY time""")], w=12, options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Accepted vs handled connections / s, requests / s", [sql("""
    SELECT time, metric,
           greatest(v - lagInFrame(v, 1, v) OVER (PARTITION BY metric ORDER BY time
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0) / $__interval_s AS per_s
    FROM (SELECT $__timeInterval(TimeUnix) AS time, MetricName AS metric, max(Value) AS v
          FROM otel.otel_metrics_sum
          WHERE $__timeFilter(TimeUnix)
            AND MetricName IN ('nginx.connections_accepted', 'nginx.connections_handled', 'nginx.requests')
          GROUP BY time, metric)
    ORDER BY time""")], w=12, options=LEGEND, field={"custom": TS},
    description="accepted > handled means nginx is dropping connections (worker_connections limit).")
d.row("Load balancing, retries, canary")
d.panel("timeseries", "Requests / s per gateway pod (least_conn)", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time,
           concat(LogAttributes['route'], ' ', LogAttributes['upstream_addr']) AS pod,
           count() / $__interval_s AS rps
    {NGX} AND LogAttributes['route'] != '' AND LogAttributes['upstream_addr'] NOT LIKE '%,%'
      AND LogAttributes['upstream_addr'] LIKE '%:%'
    GROUP BY time, pod ORDER BY time""")], w=12, unit="reqps", options=LEGEND, field={"custom": TS},
    description="nginx resolves the headless Service and balances across individual pods.")
d.panel("timeseries", "Retried requests / s (tried more than one pod)", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time,
           countIf(LogAttributes['upstream_addr'] LIKE '%,%') / $__interval_s AS retries
    {NGX} GROUP BY time ORDER BY time""")], w=12, options=LEGEND, field={"custom": TS},
    description="proxy_next_upstream on connection error / 503. Stays at 0 while pods shut down gracefully.")
d.panel("timeseries", "Canary vs stable: 5xx %", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time, LogAttributes['route'] AS pool,
           countIf(LogAttributes['status'] LIKE '5%') * 100 / count() AS err_pct
    {NGX} AND LogAttributes['uri'] LIKE '/api/checkout%' AND LogAttributes['route'] != ''
    GROUP BY time, pool ORDER BY time""")], w=12, unit="percent", options=LEGEND, field={"custom": TS})
d.panel("timeseries", "Canary vs stable: p50 latency", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time, LogAttributes['route'] AS pool,
           quantile(0.5)({RT}) * 1000 AS p50_ms
    {NGX} AND LogAttributes['uri'] LIKE '/api/checkout%' AND LogAttributes['route'] != ''
    GROUP BY time, pool ORDER BY time""")], w=12, unit="ms", options=LEGEND, field={"custom": TS})
d.row("Cache, rate limiting, TLS")
d.panel("timeseries", "Catalog cache results / s", [sql(f"""
    SELECT $__timeInterval(Timestamp) AS time, LogAttributes['cache'] AS cache, count() / $__interval_s AS rps
    {NGX} AND LogAttributes['uri'] LIKE '/api/catalog%' AND LogAttributes['cache'] != ''
    GROUP BY time, cache ORDER BY time""")], w=8, unit="reqps", options=LEGEND,
    field={"custom": {**TS, "stacking": {"mode": "normal"}, "fillOpacity": 40}})
d.panel("stat", "Cache hit ratio", [sql(f"""
    SELECT round(countIf(LogAttributes['cache'] = 'HIT') * 100 / greatest(count(), 1), 1) AS hit_pct
    {NGX} AND LogAttributes['uri'] LIKE '/api/catalog%' AND LogAttributes['cache'] != ''""", "table")],
    w=4, h=8, unit="percent", field={"thresholds": {"mode": "absolute", "steps": [
        {"color": "red", "value": None}, {"color": "orange", "value": 50}, {"color": "green", "value": 80}]}})
d.panel("table", "Rate-limited clients (429)", [sql(f"""
    SELECT LogAttributes['remote_addr'] AS client, countIf(LogAttributes['status'] = '429') AS rejected,
           count() AS total, round(rejected * 100 / total, 1) AS rejected_pct
    {NGX} GROUP BY client HAVING rejected > 0 ORDER BY rejected DESC""", "table")], w=6, h=8)
d.panel("piechart", "HTTP vs HTTPS", [sql(f"""
    SELECT if(LogAttributes['scheme'] = 'https', concat('https ', LogAttributes['ssl_protocol']), 'http') AS scheme,
           count() AS requests
    {NGX} GROUP BY scheme""", "table")], w=6, h=8,
    options={"reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
             "pieType": "donut", "legend": {"displayMode": "table", "placement": "right", "values": ["value"]}})
d.row("Breakdowns")
d.panel("table", "Top URIs", [sql(f"""
    SELECT LogAttributes['method'] AS method, LogAttributes['uri'] AS uri, count() AS requests,
           round(countIf(LogAttributes['status'] LIKE '5%') * 100 / count(), 2) AS err_5xx_pct,
           round(quantile(0.95)({RT}) * 1000, 1) AS p95_ms
    {NGX} GROUP BY method, uri ORDER BY requests DESC LIMIT 20""", "table")], w=12, h=8)
d.panel("table", "Top clients", [sql(f"""
    SELECT LogAttributes['remote_addr'] AS client, any(LogAttributes['user_agent']) AS user_agent,
           count() AS requests, sum(toUInt64OrZero(LogAttributes['bytes_sent'])) AS bytes
    {NGX} GROUP BY client ORDER BY requests DESC LIMIT 20""", "table")], w=12, h=8)
d.panel("table", "Recent 5xx responses (click-through via trace_id)", [sql(f"""
    SELECT Timestamp AS time, LogAttributes['status'] AS status, LogAttributes['upstream_status'] AS upstream,
           LogAttributes['uri'] AS uri, round({RT} * 1000, 1) AS ms, TraceId AS trace_id
    {NGX} AND LogAttributes['status'] LIKE '5%' ORDER BY Timestamp DESC LIMIT 50""", "table")], w=24, h=8)
d.row("Access log")
d.panel("logs", "nginx access log", [sql("""
    SELECT Timestamp AS timestamp, Body AS body, SeverityText AS level, TraceId AS trace_id
    FROM otel.otel_logs WHERE $__timeFilter(Timestamp) AND ServiceName = 'nginx'
    ORDER BY Timestamp DESC LIMIT 300""", "logs")], w=24, h=10,
    options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True})
d.write()
# ---------------------------------------------------------------------------- eBPF (OBI)
# Kernel-level network flows from OpenTelemetry eBPF Instrumentation (tc capture).
FLOW = "MetricName = 'obi.network.flow.bytes' AND $__timeFilter(TimeUnix)"
SRC = "concat(Attributes['k8s.src.namespace'], '/', Attributes['k8s.src.owner.name'])"
DST = "concat(Attributes['k8s.dst.namespace'], '/', Attributes['k8s.dst.owner.name'])"
d = Dash("obs-ebpf", "eBPF - network flows (OBI, ClickHouse)", ["otel", "ebpf", "obi", "network", "clickhouse"])
d.row("Who talks to whom (from the kernel, no app instrumentation)")
d.panel("table", "Service map: flows in the time range", [sql(f"""
    SELECT src, dst, formatReadableSize(sum(delta)) AS bytes, round(sum(delta) / greatest(dateDiff('second', $__fromTime, $__toTime), 1)) AS bytes_per_s
    FROM (SELECT {SRC} AS src, {DST} AS dst, Attributes AS series, max(Value) - min(Value) AS delta
          FROM otel.otel_metrics_sum WHERE {FLOW} GROUP BY src, dst, series)
    GROUP BY src, dst HAVING sum(delta) > 0 ORDER BY sum(delta) DESC LIMIT 40""", "table")], w=24, h=10,
    description="Each row is a pod-owner -> pod-owner flow seen by eBPF traffic-control hooks. '/obs' is the node itself.")
d.panel("timeseries", "Top flows (bytes/s)", [sql(f"""
    SELECT time, flow, sum(rate) AS bytes_per_s FROM (
      SELECT time, flow, series,
             greatest(v - lagInFrame(v, 1, v) OVER (PARTITION BY series ORDER BY time
                      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0) / $__interval_s AS rate
      FROM (SELECT $__timeInterval(TimeUnix) AS time, concat({SRC}, ' -> ', {DST}) AS flow,
                   Attributes AS series, max(Value) AS v
            FROM otel.otel_metrics_sum WHERE {FLOW} GROUP BY time, flow, series))
    WHERE flow IN (SELECT concat({SRC}, ' -> ', {DST}) AS f FROM otel.otel_metrics_sum WHERE {FLOW}
                   GROUP BY f ORDER BY max(Value) DESC LIMIT 8)
    GROUP BY time, flow ORDER BY time""")], w=24, h=9, unit="Bps", options=LEGEND, field={"custom": TS})
d.row("Traffic by namespace")
d.panel("table", "Namespace -> namespace", [sql(f"""
    SELECT src_ns, dst_ns, formatReadableSize(sum(delta)) AS bytes FROM (
      SELECT Attributes['k8s.src.namespace'] AS src_ns, Attributes['k8s.dst.namespace'] AS dst_ns,
             Attributes AS series, max(Value) - min(Value) AS delta
      FROM otel.otel_metrics_sum WHERE {FLOW} GROUP BY src_ns, dst_ns, series)
    GROUP BY src_ns, dst_ns HAVING sum(delta) > 0 ORDER BY sum(delta) DESC""", "table")], w=12, h=9)
d.panel("bargauge", "Top talkers (bytes sent)", [sql(f"""
    SELECT src AS talker, sum(delta) AS bytes FROM (
      SELECT {SRC} AS src, Attributes AS series, max(Value) - min(Value) AS delta
      FROM otel.otel_metrics_sum WHERE {FLOW} GROUP BY src, series)
    GROUP BY talker ORDER BY bytes DESC LIMIT 10""", "table")], w=12, h=9, unit="bytes",
    options={"orientation": "horizontal", "displayMode": "gradient",
             "reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""}})
d.panel("text", "About eBPF on this machine", [], w=24, h=4, options={"mode": "markdown", "content": (
    "**Network flows** come from OBI's kernel traffic-control (tc) hooks and work on minikube.\n\n"
    "**Zero-code request tracing** (spans / RED metrics for Grafana, Prometheus, ClickHouse) needs the "
    "`security_socket_accept` kernel hook, which the WSL2 kernel lacks (`CONFIG_SECURITY_NETWORK` off). "
    "It works on a standard Linux kernel, e.g. the Lambda GPU node (`deploy/cloud/lambda`).")})
d.write()