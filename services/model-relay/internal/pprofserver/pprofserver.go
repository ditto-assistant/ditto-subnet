// Package pprofserver runs net/http/pprof on a loopback-only listener.
//
// The listener is deliberately separate from the public request plane. Its
// default port is the service's main port plus PortOffset, so the two Platform
// relay slots map 8010 -> 11010 and 8011 -> 11011 without another per-slot
// setting. Operators reach it only through the GCP IAP SSH wrapper documented
// in docs/PERFORMANCE-PROFILING.md.
package pprofserver

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/pprof"
	"os"
	"runtime"
	"strconv"
	"time"
)

// PortOffset maps a service's main HTTP port to its loopback pprof port.
const PortOffset = 3000

// Start launches a best-effort pprof listener and returns immediately.
//
// PPROF_PORT overrides the derived port. PPROF_DISABLE=true (or 1) disables
// the listener. Optional PPROF_BLOCK_RATE and PPROF_MUTEX_FRACTION enable the
// corresponding contention profiles; both remain off by default because they
// add runtime overhead. Profiling must never make the request plane fail boot.
func Start(ctx context.Context, logger *slog.Logger, service string, mainPort int) {
	if disabled := os.Getenv("PPROF_DISABLE"); disabled == "true" || disabled == "1" {
		logger.Info("pprof disabled", slog.String("service", service))
		return
	}

	port, err := resolvePort(mainPort, os.Getenv("PPROF_PORT"))
	if err != nil {
		logger.Warn("pprof not started", slog.String("service", service), slog.String("error", err.Error()))
		return
	}
	if value, parseErr := strconv.Atoi(os.Getenv("PPROF_BLOCK_RATE")); parseErr == nil && value > 0 {
		runtime.SetBlockProfileRate(value)
	}
	if value, parseErr := strconv.Atoi(os.Getenv("PPROF_MUTEX_FRACTION")); parseErr == nil && value > 0 {
		runtime.SetMutexProfileFraction(value)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/debug/pprof/", pprof.Index)
	mux.HandleFunc("/debug/pprof/cmdline", pprof.Cmdline)
	mux.HandleFunc("/debug/pprof/profile", pprof.Profile)
	mux.HandleFunc("/debug/pprof/symbol", pprof.Symbol)
	mux.HandleFunc("/debug/pprof/trace", pprof.Trace)
	server := &http.Server{
		Addr:              net.JoinHostPort("127.0.0.1", strconv.Itoa(port)),
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if shutdownErr := server.Shutdown(shutdownCtx); shutdownErr != nil {
			logger.Warn("pprof shutdown failed", slog.String("service", service), slog.String("error", shutdownErr.Error()))
		}
	}()
	go func() {
		logger.Info("pprof listener starting", slog.String("service", service), slog.String("addr", server.Addr))
		if serveErr := server.ListenAndServe(); serveErr != nil && serveErr != http.ErrServerClosed {
			logger.Warn("pprof listener failed", slog.String("service", service), slog.String("addr", server.Addr), slog.String("error", serveErr.Error()))
		}
	}()
}

func resolvePort(mainPort int, override string) (int, error) {
	port := mainPort + PortOffset
	if override != "" {
		parsed, err := strconv.Atoi(override)
		if err != nil {
			return 0, fmt.Errorf("invalid PPROF_PORT %q", override)
		}
		port = parsed
	}
	if port < 1 || port > 65535 {
		return 0, fmt.Errorf("pprof port %d is outside 1..65535", port)
	}
	return port, nil
}
