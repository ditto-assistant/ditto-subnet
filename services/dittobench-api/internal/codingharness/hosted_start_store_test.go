package codingharness

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
	"time"
)

func startCommandConfig() HostedStartCommandConfig {
	return HostedStartCommandConfig{PythonExecutable: "/approved/python", WorkerID: "10000000-0000-4000-8000-000000000001", PostgresEnvironment: []string{"POSTGRES_HOST=localhost", "POSTGRES_PORT=15433", "POSTGRES_USER=test", "POSTGRES_PASSWORD=secret", "POSTGRES_DB=test"}}
}

func TestHostedStartCommandConfigurationIsPrivateAndCannotInjectCode(t *testing.T) {
	config := startCommandConfig()
	store, err := NewHostedStartCommand(config)
	if err != nil {
		t.Fatal(err)
	}
	config.PostgresEnvironment[0] = "PYTHONPATH=/untrusted"
	if store.config.PostgresEnvironment[0] != "POSTGRES_HOST=localhost" {
		t.Fatal("caller mutated owned environment")
	}
	for _, v := range []any{store, config} {
		if _, err := json.Marshal(v); err == nil {
			t.Fatal("credentials serialized")
		}
		if strings.Contains(fmt.Sprintf("%+v", v), "secret") {
			t.Fatal("credentials logged")
		}
	}
	for _, entry := range []string{"PYTHONPATH=/tmp", "LD_PRELOAD=/tmp/code", "PATH=/tmp", "POSTGRES_PASSWORD=second"} {
		bad := startCommandConfig()
		bad.PostgresEnvironment = append(bad.PostgresEnvironment, entry)
		if _, err := NewHostedStartCommand(bad); err == nil {
			t.Fatal("unsafe environment accepted")
		}
	}
	bad := startCommandConfig()
	bad.PythonExecutable = "python"
	if _, err := NewHostedStartCommand(bad); err == nil {
		t.Fatal("ambient executable accepted")
	}
}

func TestHostedStartOutputCannotBypassBoundThroughReaderFrom(t *testing.T) {
	output := &boundedStartOutput{}
	if _, ok := any(output).(io.ReaderFrom); ok {
		t.Fatal("embedded buffer bypasses output bound")
	}
	if _, err := io.Copy(output, bytes.NewReader(make([]byte, 2048))); err == nil {
		t.Fatal("oversized helper output accepted")
	}
	if len(output.Bytes()) > 1024 {
		t.Fatal("output exceeded limit")
	}
}

func TestHostedStartAcknowledgementMustBindExactRequest(t *testing.T) {
	digest := strings.Repeat("a", 64)
	valid := fmt.Sprintf(`{"schema":"dittobench-coding-hosted-start-result-v2","request_sha256":"%s","newly_started":true}`, digest)
	if fresh, err := parseHostedStartResult([]byte(valid), digest); err != nil || !fresh {
		t.Fatal("valid start acknowledgement rejected")
	}
	for _, body := range []string{
		strings.Replace(valid, digest, strings.Repeat("b", 64), 1),
		strings.Replace(valid, `"newly_started":true`, `"newly_started":null`, 1),
		strings.Replace(valid, `"newly_started":true`, `"newly_started":1`, 1),
		strings.Replace(valid, `"newly_started":true`, `"newly_started":true,"newly_started":false`, 1),
		valid + `{}`, strings.Repeat(" ", 1025),
	} {
		if fresh, err := parseHostedStartResult([]byte(body), digest); err == nil || fresh {
			t.Fatal("invalid acknowledgement accepted")
		}
	}
	if fresh, err := parseHostedStartResult([]byte(strings.Replace(valid, ":true", ":false", 1)), digest); err != nil || fresh {
		t.Fatal("replayed start was not denied")
	}
}

// Invoked by the Platform PostgreSQL integration test with its isolated test DB.
// No test fallback or synthesized start result is used in this path.
func TestHostedStartCommandPlatformIntegration(t *testing.T) {
	python := os.Getenv("DITTO_HOSTED_START_TEST_PYTHON")
	if python == "" {
		t.Skip("Platform integration driver is not configured")
	}
	var binding HostedBinding
	if json.Unmarshal([]byte(os.Getenv("DITTO_HOSTED_START_TEST_BINDING")), &binding) != nil {
		t.Fatal("invalid synthetic binding")
	}
	config := HostedStartCommandConfig{PythonExecutable: python, WorkerID: binding.WorkerID}
	for _, key := range []string{"POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} {
		config.PostgresEnvironment = append(config.PostgresEnvironment, key+"="+os.Getenv(key))
	}
	store, err := NewHostedStartCommand(config)
	if err != nil {
		t.Fatal("invalid test store config")
	}
	factory, runtime, _ := hostedFixture(t, store)
	binding.ImageURL = "https://screened.invalid/image.tar?synthetic=1"
	binding.ImageExpiresAt = time.Now().Add(time.Minute)
	h, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal("acquire failed")
	}
	cleanupHosted(t, h)
	if err := h.Activate(t.Context()); err != nil {
		t.Fatal("real Platform committed-start handoff failed")
	}
	if runtime.started != 1 {
		t.Fatal("candidate not launched after commit")
	}
	if err := h.Destroy(t.Context()); err != nil {
		t.Fatal(err)
	}
	replay, err := factory.Acquire(t.Context(), binding)
	if err != nil {
		t.Fatal(err)
	}
	cleanupHosted(t, replay)
	if err := replay.Activate(t.Context()); err == nil {
		t.Fatal("durable replay launched candidate")
	}
	if runtime.started != 1 {
		t.Fatal("replay reached runtime")
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	if fresh, err := store.CommitStart(cancelled, h.binding, h.instanceID); err == nil || fresh {
		t.Fatal("cancelled handoff accepted")
	}
}
