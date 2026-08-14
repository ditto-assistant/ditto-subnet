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

const PortOffset = 3000

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
		Addr:              net.JoinHostPort("127.0.0.1", strconv.Itoa(port)),
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()
	go func() {
		log.Printf("pprof: listener starting service=%s addr=%s", service, server.Addr)
		if serveErr := server.ListenAndServe(); serveErr != nil && serveErr != http.ErrServerClosed {
			log.Printf("pprof: listener failed service=%s addr=%s error=%v", service, server.Addr, serveErr)
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
