// Command gpu-exporter collects NVIDIA GPU metrics by polling nvidia-smi and
//
//   - pushes them over OTLP/gRPC (default: the local host agent on 127.0.0.1:4317)
//   - also serves them in Prometheus text format on /metrics for debugging
//
// Metric names follow NVIDIA DCGM exporter conventions (DCGM_FI_DEV_*) so the
// same dashboards work with dcgm-exporter on Linux GPU nodes.
//
//	gpu-exporter -listen 127.0.0.1:9835 -interval 5s -otlp 127.0.0.1:4317
package main

import (
	"bytes"
	"context"
	"encoding/csv"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
)

// field maps one nvidia-smi query column to a DCGM-style metric.
type field struct {
	query  string
	metric string
	help   string
	unit   string
}

// Label columns come first, in this order.
var labelQueries = []string{"index", "uuid", "name"}

var fields = []field{
	{"utilization.gpu", "DCGM_FI_DEV_GPU_UTIL", "GPU utilization (in %).", ""},
	{"utilization.memory", "DCGM_FI_DEV_MEM_COPY_UTIL", "Memory utilization (in %).", ""},
	{"memory.used", "DCGM_FI_DEV_FB_USED", "Framebuffer memory used (in MiB).", ""},
	{"memory.free", "DCGM_FI_DEV_FB_FREE", "Framebuffer memory free (in MiB).", ""},
	{"memory.total", "DCGM_FI_DEV_FB_TOTAL", "Framebuffer memory total (in MiB).", ""},
	{"temperature.gpu", "DCGM_FI_DEV_GPU_TEMP", "GPU temperature (in C).", ""},
	{"power.draw", "DCGM_FI_DEV_POWER_USAGE", "Power draw (in W).", ""},
	{"power.limit", "DCGM_FI_DEV_POWER_LIMIT", "Power limit (in W).", ""},
	{"clocks.sm", "DCGM_FI_DEV_SM_CLOCK", "SM clock frequency (in MHz).", ""},
	{"clocks.mem", "DCGM_FI_DEV_MEM_CLOCK", "Memory clock frequency (in MHz).", ""},
	{"fan.speed", "DCGM_FI_DEV_FAN_SPEED", "Fan speed (in %).", ""},
	{"pcie.link.gen.current", "DCGM_FI_DEV_PCIE_LINK_GEN", "Current PCIe link generation.", ""},
}

// sample is one GPU's reading from a single poll.
type sample struct {
	gpu, uuid, model string
	values           map[string]float64 // metric name -> value (missing = N/A)
}

type exporter struct {
	smi      string
	hostname string

	mu      sync.RWMutex
	samples []sample
	lastErr error
	pollDur float64
}

func main() {
	listen := flag.String("listen", "127.0.0.1:9835", "Prometheus /metrics listen address (empty to disable)")
	interval := flag.Duration("interval", 5*time.Second, "poll interval")
	smi := flag.String("nvidia-smi", defaultSMI(), "path to nvidia-smi")
	otlp := flag.String("otlp", envOr("GPU_EXPORTER_OTLP_ENDPOINT", "127.0.0.1:4317"), "OTLP/gRPC endpoint (empty to disable)")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	host, _ := os.Hostname()
	e := &exporter{smi: *smi, hostname: host}
	e.poll()
	go func() {
		t := time.NewTicker(*interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				e.poll()
			}
		}
	}()

	if *otlp != "" {
		shutdown, err := e.startOTLP(ctx, *otlp, *interval)
		if err != nil {
			slog.Error("otlp setup failed", "err", err)
			os.Exit(1)
		}
		defer func() {
			c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = shutdown(c)
		}()
		slog.Info("pushing OTLP metrics", "endpoint", *otlp, "interval", *interval)
	}

	if *listen != "" {
		mux := http.NewServeMux()
		mux.HandleFunc("/metrics", e.servePrometheus)
		mux.HandleFunc("/healthz", e.serveHealth)
		srv := &http.Server{Addr: *listen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
		go func() {
			<-ctx.Done()
			_ = srv.Close()
		}()
		slog.Info("serving Prometheus metrics", "addr", *listen, "nvidia_smi", *smi)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("server failed", "err", err)
		}
		return
	}
	<-ctx.Done()
}

// ---- polling ----------------------------------------------------------------

func (e *exporter) poll() {
	queries := append([]string{}, labelQueries...)
	for _, f := range fields {
		queries = append(queries, f.query)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	start := time.Now()
	out, err := exec.CommandContext(ctx, e.smi,
		"--query-gpu="+strings.Join(queries, ","), "--format=csv,noheader,nounits").Output()
	dur := time.Since(start).Seconds()

	var samples []sample
	if err == nil {
		samples, err = parse(out)
	}
	if err != nil {
		slog.Warn("nvidia-smi poll failed", "err", err)
	}
	e.mu.Lock()
	e.samples, e.lastErr, e.pollDur = samples, err, dur
	e.mu.Unlock()
}

func parse(out []byte) ([]sample, error) {
	r := csv.NewReader(bytes.NewReader(out))
	r.TrimLeadingSpace = true
	rows, err := r.ReadAll()
	if err != nil {
		return nil, fmt.Errorf("parse nvidia-smi csv: %w", err)
	}
	nl := len(labelQueries)
	samples := make([]sample, 0, len(rows))
	for _, row := range rows {
		if len(row) != nl+len(fields) {
			return nil, fmt.Errorf("unexpected column count %d", len(row))
		}
		s := sample{gpu: row[0], uuid: row[1], model: row[2], values: map[string]float64{}}
		for i, f := range fields {
			if v, err := strconv.ParseFloat(strings.TrimSpace(row[nl+i]), 64); err == nil {
				s.values[f.metric] = v // "[N/A]" / "[Not Supported]" are skipped
			}
		}
		samples = append(samples, s)
	}
	return samples, nil
}

// ---- OTLP push ----------------------------------------------------------------

func (e *exporter) startOTLP(ctx context.Context, endpoint string, interval time.Duration) (func(context.Context) error, error) {
	exp, err := otlpmetricgrpc.New(ctx, otlpmetricgrpc.WithEndpoint(endpoint), otlpmetricgrpc.WithInsecure())
	if err != nil {
		return nil, err
	}
	res, err := resource.New(ctx,
		resource.WithHost(),
		resource.WithAttributes(
			attribute.String("service.name", "gpu-exporter"),
			attribute.String("host.role", "baremetal"),
		),
	)
	if err != nil {
		return nil, err
	}
	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exp, sdkmetric.WithInterval(interval))),
	)
	meter := mp.Meter("obsdemo/gpu-exporter")

	gauges := make([]metric.Observable, 0, len(fields)+1)
	byName := map[string]metric.Float64ObservableGauge{}
	for _, f := range fields {
		g, err := meter.Float64ObservableGauge(f.metric, metric.WithDescription(f.help), metric.WithUnit(f.unit))
		if err != nil {
			return nil, err
		}
		byName[f.metric] = g
		gauges = append(gauges, g)
	}
	up, err := meter.Float64ObservableGauge("gpu_exporter_up", metric.WithDescription("Whether the last nvidia-smi poll succeeded."))
	if err != nil {
		return nil, err
	}
	gauges = append(gauges, up)

	_, err = meter.RegisterCallback(func(_ context.Context, o metric.Observer) error {
		e.mu.RLock()
		defer e.mu.RUnlock()
		upV := 1.0
		if e.lastErr != nil {
			upV = 0
		}
		o.ObserveFloat64(up, upV)
		for _, s := range e.samples {
			attrs := metric.WithAttributes(
				attribute.String("gpu", s.gpu),
				attribute.String("UUID", s.uuid),
				attribute.String("modelName", s.model),
				attribute.String("Hostname", e.hostname),
			)
			for name, v := range s.values {
				o.ObserveFloat64(byName[name], v, attrs)
			}
		}
		return nil
	}, gauges...)
	if err != nil {
		return nil, err
	}
	return mp.Shutdown, nil
}

// ---- Prometheus text ------------------------------------------------------------

func (e *exporter) servePrometheus(w http.ResponseWriter, _ *http.Request) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	var buf bytes.Buffer
	for _, f := range fields {
		fmt.Fprintf(&buf, "# HELP %s %s\n# TYPE %s gauge\n", f.metric, f.help, f.metric)
		for _, s := range e.samples {
			if v, ok := s.values[f.metric]; ok {
				fmt.Fprintf(&buf, "%s{gpu=%q,UUID=%q,modelName=%q,Hostname=%q} %g\n",
					f.metric, s.gpu, s.uuid, s.model, e.hostname, v)
			}
		}
	}
	up := 1.0
	if e.lastErr != nil {
		up = 0
	}
	fmt.Fprintf(&buf, "# HELP gpu_exporter_up Whether the last nvidia-smi poll succeeded.\n# TYPE gpu_exporter_up gauge\ngpu_exporter_up{Hostname=%q} %g\n", e.hostname, up)
	fmt.Fprintf(&buf, "# HELP gpu_exporter_poll_duration_seconds Duration of the last nvidia-smi poll.\n# TYPE gpu_exporter_poll_duration_seconds gauge\ngpu_exporter_poll_duration_seconds{Hostname=%q} %g\n", e.hostname, e.pollDur)
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	_, _ = w.Write(buf.Bytes())
}

func (e *exporter) serveHealth(w http.ResponseWriter, _ *http.Request) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	if e.lastErr != nil {
		http.Error(w, e.lastErr.Error(), http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
}

// ---- helpers ------------------------------------------------------------------

func defaultSMI() string {
	if runtime.GOOS == "windows" {
		if p := `C:\Windows\System32\nvidia-smi.exe`; fileExists(p) {
			return p
		}
	}
	return "nvidia-smi"
}

func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
