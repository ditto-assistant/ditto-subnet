package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scorer"
)

// runScope classifies the canonical /v1/score path (dataset_sha256 pinned) as
// SCORED and a plain /v1/submit run_size as PRACTICE. This is the scored-vs-
// practice boundary that drives mandatory observed execution and the closed
// parser free points.
func TestRunScope(t *testing.T) {
	if got := runScope(submitRequest{RunSize: "full", HarnessURL: "http://h"}); got != scorer.ScopePractice {
		t.Fatalf("run_size submit without dataset_sha256 must be practice, got %v", got)
	}
	if got := runScope(submitRequest{RunSize: "full", HarnessURL: "http://h", ExpectedDatasetSHA256: "abc123"}); got != scorer.ScopeScored {
		t.Fatalf("pinned dataset_sha256 (/v1/score) must be scored, got %v", got)
	}
	// Whitespace-only hash is not a real pin.
	if got := runScope(submitRequest{RunSize: "full", ExpectedDatasetSHA256: "   "}); got != scorer.ScopePractice {
		t.Fatalf("blank dataset_sha256 must be practice, got %v", got)
	}
}

func TestLoopbackHarnessSourceIP(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		want string
		ok   bool
	}{
		{name: "ipv4", raw: "http://127.0.0.1:19001", want: "127.0.0.1", ok: true},
		{name: "ipv6", raw: "http://[::1]:19001", want: "::1", ok: true},
		{name: "remote ip", raw: "https://192.0.2.1/harness", ok: false},
		{name: "hostname is not silently resolved", raw: "http://localhost:19001", ok: false},
		{name: "invalid", raw: "://bad", ok: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, ok := loopbackHarnessSourceIP(test.raw)
			if got != test.want || ok != test.ok {
				t.Fatalf("loopbackHarnessSourceIP(%q) = (%q, %v), want (%q, %v)", test.raw, got, ok, test.want, test.ok)
			}
		})
	}
}
