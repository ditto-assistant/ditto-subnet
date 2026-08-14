// Package pprofserver runs net/http/pprof on a loopback-only listener.
package pprofserver

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/pprof"
	"os"
	"runtime"
	"strconv"
	"time"
)

// PortOffset maps the process's main HTTP port to its pprof port.
const PortOffset = 3000

// Start launches a best-effort loopback pprof listener and returns immediately.
// PPROF_PORT overrides the derived port; PPROF_DISABLE=true (or 1) disables it.
// Block and mutex profiling remain opt-in because they add runtime overhead.
func Start(ctx context.Context, service string, mainPort int) {
	if disabled := os.Getenv("PPROF_DISABLE"); disabled == "true" || disabled == "1" {
		log.Printf("pprof: disabled service=%s", service)
		return
	}
	port, err := resolvePort(mainPort, os.Getenv("PPROF_PORT"))
	if err != nil {
		log.Printf("pprof: not started service=%s error=%v", service, err)
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
		Addr:              listenAddress(port),
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if shutdownErr := server.Shutdown(shutdownCtx); shutdownErr != nil {
			log.Printf("pprof: shutdown failed service=%s error=%v", service, shutdownErr)
		}
	}()
	go func() {
		log.Printf("pprof: listener starting service=%s addr=%s", service, server.Addr)
		if serveErr := server.ListenAndServe(); serveErr != nil && serveErr != http.ErrServerClosed {
			log.Printf("pprof: listener failed service=%s addr=%s error=%v", service, server.Addr, serveErr)
		}
	}()
}

func listenAddress(port int) string {
	// Keep this non-configurable: pprof can disclose runtime and application
	// details, so operators must enter through the SSH collection tooling.
	return net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
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
