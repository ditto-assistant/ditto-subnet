// Binary dittobench-coding-executor-scorer is the immutable, dedicated
// artifact for a future coding executor host. It deliberately exposes only a
// Unix-domain liveness endpoint and refuses to start unless an operator enables
// its future deployment profile. It owns no ticket, wallet, provider, Platform
// credential, Docker client, scorer run, or TCP listener.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/ditto-assistant/dittobench-api/internal/release"
)

const (
	defaultSocketPath = "/run/ditto-coding-scorer/control.sock"
	enableEnvironment = "DITTOBENCH_CODING_EXECUTOR_SCORER_ENABLED"
)

type configuration struct {
	socketPath string
	enabled    bool
}

func configurationFromEnvironment(socketPath string, getenv func(string) string) (configuration, error) {
	if socketPath == "" || !filepath.IsAbs(socketPath) || filepath.Clean(socketPath) != defaultSocketPath {
		return configuration{}, errors.New("coding executor scorer socket path is invalid")
	}
	if strings.EqualFold(strings.TrimSpace(getenv(enableEnvironment)), "true") {
		return configuration{socketPath: socketPath, enabled: true}, nil
	}
	return configuration{}, errors.New("coding executor scorer is disabled")
}

func controlMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(response http.ResponseWriter, _ *http.Request) {
		response.Header().Set("Cache-Control", "no-store")
		response.WriteHeader(http.StatusNoContent)
	})
	return mux
}

func listenUnix(path string) (net.Listener, error) {
	info, err := os.Lstat(path)
	if err == nil {
		metadata, ok := info.Sys().(*syscall.Stat_t)
		if !ok || info.Mode()&os.ModeSocket == 0 || info.Mode()&os.ModeSymlink != 0 || metadata.Uid != uint32(os.Geteuid()) {
			return nil, errors.New("coding executor scorer socket cannot be replaced")
		}
		if err := os.Remove(path); err != nil {
			return nil, errors.New("coding executor scorer stale socket cannot be removed")
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, errors.New("coding executor scorer socket is unavailable")
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, errors.New("coding executor scorer socket cannot listen")
	}
	if err := os.Chmod(path, 0o600); err != nil {
		_ = listener.Close()
		return nil, errors.New("coding executor scorer socket mode cannot be set")
	}
	return listener, nil
}

func main() {
	socketPath := flag.String("socket", defaultSocketPath, "fixed Unix control socket")
	version := flag.Bool("version", false, "print immutable artifact provenance")
	flag.Parse()
	if *version {
		if err := json.NewEncoder(os.Stdout).Encode(release.Resolve(os.Getenv)); err != nil {
			fmt.Fprintln(os.Stderr, "coding executor scorer cannot report version")
			os.Exit(111)
		}
		return
	}
	config, err := configurationFromEnvironment(*socketPath, os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "coding executor scorer is not enabled")
		os.Exit(78)
	}
	listener, err := listenUnix(config.socketPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "coding executor scorer socket is unavailable")
		os.Exit(111)
	}
	defer listener.Close()
	if err := http.Serve(listener, controlMux()); err != nil && !errors.Is(err, http.ErrServerClosed) {
		fmt.Fprintln(os.Stderr, "coding executor scorer stopped")
		os.Exit(111)
	}
}
