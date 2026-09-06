package codinghostedworker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

// Invoked by the Python integration test against real PostgreSQL, encrypted
// input retrieval, both evidence publishers and the live local control server.
// Candidate transport and provider responses are synthetic; no live model or
// candidate container is launched. The assignment is already durably started.
func TestPlatformControlAdapterIntegration(t *testing.T) {
	path := os.Getenv("DITTO_HOSTED_CONTROL_TEST")
	if path == "" {
		t.Skip("Platform integration not configured")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("missing integration config")
	}
	var config struct {
		Socket       string        `json:"socket"`
		Token        []byte        `json:"token"`
		Config       ControlConfig `json:"control"`
		Artifact     string        `json:"artifact"`
		Agent        string        `json:"agent"`
		MinerBody    []byte        `json:"miner_body"`
		Output       string        `json:"output"`
		Grading      bool          `json:"grading"`
		GradingFault string        `json:"grading_fault"`
	}
	// ControlConfig intentionally refuses MarshalJSON but accepts private decoding.
	if json.Unmarshal(raw, &config) != nil {
		t.Fatal("invalid integration config")
	}
	config.Config.SocketPath, config.Config.Token = config.Socket, config.Token
	client, err := NewControlClient(config.Config)
	if err != nil {
		t.Fatal("control client config rejected")
	}
	if config.Grading {
		client.testGrader = func(_ context.Context, m codinggrader.HostedManifest) (codinggrader.Executor, error) {
			return &scriptedGrader{manifest: m, failPreflight: config.GradingFault == "preflight"}, nil
		}
	}
	expected := config.Config.Expected
	binding := codingharness.HostedBinding{EvaluationID: expected.EvaluationID, AttemptID: expected.AttemptID, WorkerID: expected.WorkerID, AssignmentSHA256: expected.AssignmentSHA256, AgentID: config.Agent, AgentArtifactSHA256: config.Artifact, ProfileCapabilityID: "hosted-" + expected.AttemptID, Deadline: time.Unix(expected.DeadlineUnix, 0), ScreenedImageSHA256: strings.Repeat("b", 64), ScreenedImageID: "sha256:" + strings.Repeat("c", 64), ScreenedImageRef: "ditto-screen/" + config.Agent + ":latest", ScreenedImageSize: 1, ScreeningPolicyVersion: 11, ImageURL: "https://storage.invalid/image.tar?signature=synthetic", ImageExpiresAt: time.Now().Add(time.Minute)}
	f := &fixture{runStarted: make(chan struct{})}
	registry := codingsource.NewRegistry(nil)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	router, err := codingsource.NewRouter(codingsource.RouterConfig{Listener: routeListener{listener}, Registry: registry, PublicBaseURL: fmt.Sprintf("http://host.docker.internal:%d", listener.Addr().(*net.TCPAddr).Port)})
	if err != nil {
		t.Fatal(err)
	}
	defer router.Close(context.Background())
	routeCall := func(ctx context.Context, target string, body []byte) *httptest.ResponseRecorder {
		parsed, err := url.Parse(target)
		if err != nil {
			t.Fatal(err)
		}
		request := httptest.NewRequest("POST", target, bytes.NewReader(body)).WithContext(ctx)
		request.Host = parsed.Host
		request.RemoteAddr = "172.21.0.5:9001"
		request.Header.Set("Content-Type", "application/json")
		response := httptest.NewRecorder()
		router.ServeHTTP(response, request)
		return response
	}
	transport := transportFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.Host != "127.0.0.1:8000" {
			return nil, ErrAttempt
		}
		response := httptest.NewRecorder()
		response.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/coding/health":
			value, _ := (testHarness{f}).Health(r.Context())
			_ = json.NewEncoder(response).Encode(value)
		case "/coding/seed":
			var seed codingcontract.HostedSeedRequest
			if json.NewDecoder(r.Body).Decode(&seed) != nil {
				return nil, ErrAttempt
			}
			value, _ := (testHarness{f}).Seed(r.Context(), seed)
			_ = json.NewEncoder(response).Encode(value)
		case "/coding/run":
			var run codingcontract.HostedRunRequest
			if json.NewDecoder(r.Body).Decode(&run) != nil {
				return nil, ErrAttempt
			}
			model := routeCall(r.Context(), run.InferenceBaseURL+"/chat/completions", config.MinerBody)
			if model.Code != 200 {
				t.Errorf("native inference failed: %d", model.Code)
				return nil, ErrAttempt
			}
			if bytes.Contains(model.Body.Bytes(), []byte("provider_evidence")) {
				t.Error("private model evidence leaked")
			}
			tool := func(id, name string, args any) codingrunner.ToolResponse {
				arguments, _ := json.Marshal(args)
				body, _ := json.Marshal(codingrunner.ToolRequest{CodingContractVersion: 2, CaseID: run.CaseID, ProfileCapabilityID: run.ProfileCapabilityID, CallID: id, Name: name, Arguments: arguments})
				result := routeCall(r.Context(), run.WorkspaceCapabilityURL, body)
				var parsed codingrunner.ToolResponse
				if result.Code != 200 || json.Unmarshal(result.Body.Bytes(), &parsed) != nil || !parsed.OK {
					t.Error("workspace tool failed")
				}
				return parsed
			}
			read := tool("read", "repo.read_file", map[string]any{"path": "app.py"})
			var value struct {
				SHA string `json:"sha256"`
			}
			if json.Unmarshal(read.Result, &value) != nil {
				return nil, ErrAttempt
			}
			edit := tool("edit", "repo.apply_patch", map[string]any{"path": "app.py", "expected_sha256": value.SHA, "replacements": []map[string]string{{"old_text": "print(1)", "new_text": "print(2)"}}})
			if !edit.OK {
				return nil, ErrAttempt
			}
			result := codingcertifier.RunResponse{CaseID: run.CaseID}
			result.FinalReport.Summary = "done"
			result.FinalReport.RemainingRisks = []string{}
			_ = json.NewEncoder(response).Encode(result)
		default:
			return nil, ErrAttempt
		}
		return response.Result(), nil
	})
	factory, err := codingharness.NewHosted(codingharness.HostedConfig{Config: codingharness.Config{Runtime: nativeRuntime{f}, Sources: registry, Transport: transport, NewInstance: func() string { return "integration-instance" }}, Starts: &nativeStarts{f: f}})
	if err != nil {
		t.Fatal(err)
	}
	executors, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{ImageRepository: "example.invalid/native", CandidateUID: 10001, CandidateGID: 10001, RequireRootless: true, RequireIsolatedDaemon: true})
	if err != nil {
		t.Fatal(err)
	}
	worker, err := New(Config{Harnesses: factory, Executors: executors, Router: router, Control: client})
	if err != nil {
		t.Fatal(err)
	}
	attempt, err := worker.Attempt(binding)
	if err != nil {
		t.Fatal(err)
	}
	authority, err := attempt.Run(t.Context())
	if err != nil {
		_ = attempt.Cleanup()
		t.Fatal("connected Platform authoring failed")
	}
	again, err := attempt.RetryFinalization(t.Context())
	if err != nil || again != authority {
		t.Fatal("exact acknowledgement replay failed")
	}
	output := map[string]any{"patch_sha256": authority.FrozenPatchSHA256, "evidence_sha256": attempt.evidenceSHA}
	if config.Grading {
		terminal, err := attempt.Grade(t.Context())
		if err != nil {
			t.Fatal("native grading/terminal failed", err)
		}
		if again, err := attempt.Grade(t.Context()); err != nil || again != terminal {
			t.Fatal("terminal byte replay failed")
		}
		output["terminal_sha256"] = terminal
		output["terminal_body"] = attempt.terminalBody
	}
	result, _ := json.Marshal(output)
	if err := os.WriteFile(config.Output, result, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := attempt.Run(t.Context()); err == nil {
		t.Fatal("candidate rerun accepted")
	}
	if err := attempt.Cleanup(); err != nil {
		t.Fatal("connected cleanup failed")
	}
}

func TestControlClientRejectsUnsafeConfiguration(t *testing.T) {
	for _, config := range []ControlConfig{{}, {SocketPath: "relative", Token: bytes.Repeat([]byte{1}, 32)}} {
		if _, err := NewControlClient(config); err == nil {
			t.Fatal("unsafe control accepted")
		}
	}
}
