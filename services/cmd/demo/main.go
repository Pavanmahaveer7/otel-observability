// Command demo runs one of the demo microservices, selected by -role:
//
//	gateway  :8080  GET  /api/checkout  -> orders
//	orders   :8081  POST /orders        -> payments
//	payments :8082  POST /pay
//	loadgen         drives traffic at the gateway
//
// Every role emits traces, metrics and logs through OpenTelemetry.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	"obsdemo/services/internal/telemetry"
)

// SERVICE_VERSION lets a canary deployment report itself as e.g. 0.2.0.
var version = env("SERVICE_VERSION", "0.1.0")

var (
	tracer = otel.Tracer("obsdemo/services")
	meter  = otel.Meter("obsdemo/services")
	client = &http.Client{Transport: otelhttp.NewTransport(http.DefaultTransport), Timeout: 5 * time.Second}
)

func main() {
	role := flag.String("role", env("ROLE", "gateway"), "gateway|orders|payments|loadgen")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	shutdown, err := telemetry.Setup(ctx, *role, version)
	if err != nil {
		slog.Error("telemetry setup failed", "err", err)
		os.Exit(1)
	}
	defer func() {
		c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = shutdown(c)
	}()

	switch *role {
	case "gateway":
		serve(ctx, ":8080", gatewayMux())
	case "orders":
		serve(ctx, ":8081", ordersMux())
	case "payments":
		serve(ctx, ":8082", paymentsMux())
	case "loadgen":
		loadgen(ctx)
	default:
		slog.Error("unknown role", "role", *role)
		os.Exit(2)
	}
}

func serve(ctx context.Context, addr string, mux *http.ServeMux) {
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	srv := &http.Server{
		Addr:              addr,
		Handler:           otelhttp.NewHandler(mux, "server", otelhttp.WithSpanNameFormatter(spanName)),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		<-ctx.Done()
		c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(c)
	}()
	slog.Info("listening", "addr", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server failed", "err", err)
	}
}

func spanName(_ string, r *http.Request) string { return r.Method + " " + r.URL.Path }

// ---- gateway ---------------------------------------------------------------

func gatewayMux() *http.ServeMux {
	ordersURL := env("ORDERS_URL", "http://orders:8081")
	// Canary knobs: a "v2" build can be made slower / buggier to compare against v1.
	extraLatency := time.Duration(envFloat("GATEWAY_EXTRA_LATENCY_MS", 0)) * time.Millisecond
	failRate := envFloat("GATEWAY_FAIL_RATE", 0)
	mux := http.NewServeMux()

	// Read-only, cacheable endpoint (nginx caches it at the edge).
	mux.HandleFunc("GET /api/catalog", func(w http.ResponseWriter, r *http.Request) {
		_, span := tracer.Start(r.Context(), "catalog.load", trace.WithAttributes(attribute.String("db.system", "postgresql")))
		time.Sleep(time.Duration(30+rand.IntN(50)) * time.Millisecond)
		span.End()
		writeJSON(w, map[string]any{
			"version": version,
			"items": []map[string]any{
				{"sku": "gpu-hours", "unit_price": 2.49},
				{"sku": "cpu-hours", "unit_price": 0.04},
				{"sku": "storage-gb", "unit_price": 0.02},
				{"sku": "egress-gb", "unit_price": 0.08},
			},
		})
	})

	mux.HandleFunc("GET /api/checkout", func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		if extraLatency > 0 {
			time.Sleep(extraLatency)
		}
		if failRate > 0 && rand.Float64() < failRate {
			span := trace.SpanFromContext(ctx)
			span.SetStatus(codes.Error, "canary bug")
			slog.ErrorContext(ctx, "checkout crashed", "version", version)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		item := []string{"gpu-hours", "cpu-hours", "storage-gb", "egress-gb"}[rand.IntN(4)]
		qty := 1 + rand.IntN(5)
		trace.SpanFromContext(ctx).SetAttributes(attribute.String("app.item", item), attribute.Int("app.qty", qty))

		status, body, err := postJSON(ctx, ordersURL+"/orders", map[string]any{"item": item, "qty": qty})
		if err != nil || status >= 400 {
			slog.WarnContext(ctx, "checkout failed", "item", item, "status", status, "err", err)
			http.Error(w, "checkout failed", http.StatusBadGateway)
			return
		}
		slog.InfoContext(ctx, "checkout ok", "item", item, "qty", qty)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	})
	return mux
}

// ---- orders ----------------------------------------------------------------

func ordersMux() *http.ServeMux {
	paymentsURL := env("PAYMENTS_URL", "http://payments:8082")
	created, _ := meter.Int64Counter("orders.created", metric.WithDescription("Orders accepted"), metric.WithUnit("{order}"))
	mux := http.NewServeMux()
	mux.HandleFunc("POST /orders", func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		var in struct {
			Item string `json:"item"`
			Qty  int    `json:"qty"`
		}
		if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		orderID := fmt.Sprintf("ord-%d", rand.Int64N(1_000_000))

		// Simulated database write as a child span.
		_, db := tracer.Start(ctx, "db.insert orders", trace.WithSpanKind(trace.SpanKindClient),
			trace.WithAttributes(attribute.String("db.system", "postgresql"), attribute.String("db.operation", "INSERT")))
		time.Sleep(time.Duration(5+rand.IntN(25)) * time.Millisecond)
		db.End()

		amount := float64(in.Qty) * (5 + rand.Float64()*45)
		status, _, err := postJSON(ctx, paymentsURL+"/pay", map[string]any{"order_id": orderID, "amount": amount})
		if err != nil || status >= 400 {
			created.Add(ctx, 1, metric.WithAttributes(attribute.String("item", in.Item), attribute.String("result", "payment_failed")))
			slog.ErrorContext(ctx, "payment failed", "order_id", orderID, "status", status, "err", err)
			http.Error(w, "payment failed", http.StatusPaymentRequired)
			return
		}
		created.Add(ctx, 1, metric.WithAttributes(attribute.String("item", in.Item), attribute.String("result", "ok")))
		slog.InfoContext(ctx, "order created", "order_id", orderID, "item", in.Item, "amount", amount)
		writeJSON(w, map[string]any{"order_id": orderID, "amount": amount})
	})
	return mux
}

// ---- payments --------------------------------------------------------------

func paymentsMux() *http.ServeMux {
	failRate := envFloat("PAYMENT_FAIL_RATE", 0.05)
	amounts, _ := meter.Float64Histogram("payments.amount", metric.WithDescription("Charged amount"), metric.WithUnit("USD"))
	mux := http.NewServeMux()
	mux.HandleFunc("POST /pay", func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		span := trace.SpanFromContext(ctx)
		var in struct {
			OrderID string  `json:"order_id"`
			Amount  float64 `json:"amount"`
		}
		if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		// Occasional slow call to make latency dashboards interesting.
		delay := 10 + rand.IntN(40)
		if rand.Float64() < 0.03 {
			delay += 400 + rand.IntN(600)
		}
		time.Sleep(time.Duration(delay) * time.Millisecond)

		if rand.Float64() < failRate {
			err := errors.New("card declined")
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
			slog.ErrorContext(ctx, "payment declined", "order_id", in.OrderID, "amount", in.Amount)
			http.Error(w, err.Error(), http.StatusPaymentRequired)
			return
		}
		amounts.Record(ctx, in.Amount)
		slog.InfoContext(ctx, "payment captured", "order_id", in.OrderID, "amount", in.Amount)
		writeJSON(w, map[string]any{"status": "captured"})
	})
	return mux
}

// ---- loadgen ---------------------------------------------------------------

func loadgen(ctx context.Context) {
	base := env("GATEWAY_URL", "http://gateway:8080")
	catalogShare := envFloat("CATALOG_SHARE", 0.25) // fraction of requests that browse the catalog
	rps := envFloat("RPS", 5)
	slog.Info("loadgen started", "target", base, "rps", rps, "catalog_share", catalogShare)
	t := time.NewTicker(time.Duration(float64(time.Second) / rps))
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			go func() {
				path, name := "/api/checkout", "loadgen checkout"
				if rand.Float64() < catalogShare {
					path, name = "/api/catalog", "loadgen catalog"
				}
				c, span := tracer.Start(ctx, name, trace.WithSpanKind(trace.SpanKindClient))
				defer span.End()
				req, _ := http.NewRequestWithContext(c, http.MethodGet, base+path, nil)
				resp, err := client.Do(req)
				if err != nil {
					span.SetStatus(codes.Error, err.Error())
					slog.WarnContext(c, "request failed", "err", err)
					return
				}
				_, _ = io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				if resp.StatusCode >= 500 {
					span.SetStatus(codes.Error, resp.Status)
				}
			}()
		}
	}
}

// ---- helpers ---------------------------------------------------------------

func postJSON(ctx context.Context, url string, v any) (int, []byte, error) {
	b, _ := json.Marshal(v)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(b))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return resp.StatusCode, body, err
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envFloat(k string, def float64) float64 {
	if f, err := strconv.ParseFloat(os.Getenv(k), 64); err == nil && f > 0 {
		return f
	}
	return def
}
