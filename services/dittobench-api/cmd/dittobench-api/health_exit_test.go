package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

func TestWaitSandboxHealthyFailsFastWithSanitizedExitState(t *testing.T) {
	httpServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "not ready", http.StatusServiceUnavailable)
	}))
	defer httpServer.Close()

	backend := &diagnosticSandbox{diagnostics: sandbox.RuntimeDiagnostics{
		StateKnown: true,
		Running:    false,
		ExitCode:   1,
	}}
	s := &server{sandbox: backend}
	started := time.Now()
	err := s.waitSandboxHealthy(
		context.Background(),
		&sandbox.Handle{BaseURL: httpServer.URL, ContainerID: "private-container"},
		time.Minute,
	)
	if err == nil || !strings.Contains(err.Error(), "exit_code=1 oom_killed=false") {
		t.Fatalf("unexpected error: %v", err)
	}
	if strings.Contains(err.Error(), "private-container") {
		t.Fatalf("private container identity leaked: %v", err)
	}
	if time.Since(started) > 2*time.Second {
		t.Fatalf("exited harness was not detected promptly: %s", time.Since(started))
	}
}

func TestWaitSandboxHealthyFailsFastOnCleanExit(t *testing.T) {
	httpServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "not ready", http.StatusServiceUnavailable)
	}))
	defer httpServer.Close()

	backend := &diagnosticSandbox{diagnostics: sandbox.RuntimeDiagnostics{
		StateKnown: true,
		Running:    false,
		ExitCode:   0,
	}}
	s := &server{sandbox: backend}
	err := s.waitSandboxHealthy(
		context.Background(),
		&sandbox.Handle{BaseURL: httpServer.URL, ContainerID: "private-container"},
		time.Minute,
	)
	if err == nil || !strings.Contains(err.Error(), "exit_code=0 oom_killed=false") {
		t.Fatalf("unexpected error: %v", err)
	}
}
