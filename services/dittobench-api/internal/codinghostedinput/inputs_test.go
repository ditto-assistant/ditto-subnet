package codinghostedinput

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func fixtureProfile() Profile {
	limits := codingrunner.DefaultLimits()
	limits.MaxBundleBytes = 4 << 20
	limits.MaxWorkspaceBytes = 16 << 20
	limits.MaxFileBytes = 4 << 20
	limits.MaxPatchBytes = 1024
	return Profile{Schema: "dittobench-coding-hosted-authoring-profile-v2", ImageDigest: "sha256:" + strings.Repeat("9", 64),
		ResourcePolicy: codinggrader.ResourcePolicy{CandidateLimits: limits, ProtectedLimits: limits, MaxCombinedDiskBytes: 1 << 30, MemoryLimitBytes: 1 << 30, ScratchLimitBytes: 512 << 20, PidsLimit: 256, CPUQuotaMillis: 2000},
		Budgets:        codingcontract.Budgets{ModelInputTokens: 10000, ModelOutputTokens: 1000, WorkspaceToolCalls: limits.MaxToolCalls, WallTimeSeconds: 600}}
}

// The Platform test passes a real encrypted/retrieved private frame and a
// separately trusted expected authority. All content is synthetic test data.
func TestPlatformAuthoringInputIntegration(t *testing.T) {
	profile := fixtureProfile()
	profileSHA, err := ProfileDigest(profile)
	if err != nil {
		t.Fatal(err)
	}
	if path := os.Getenv("DITTO_HOSTED_INPUT_PROFILE_OUT"); path != "" {
		body, _ := json.Marshal(map[string]any{"profile": profile, "sha256": profileSHA})
		if err := os.WriteFile(path, body, 0600); err != nil {
			t.Fatal(err)
		}
		return
	}
	framePath := os.Getenv("DITTO_HOSTED_INPUT_FRAME")
	if framePath == "" {
		t.Skip("Platform producer integration not configured")
	}
	metadata, err := os.ReadFile(os.Getenv("DITTO_HOSTED_INPUT_EXPECTED"))
	if err != nil {
		t.Fatal("missing test authority")
	}
	var expected Expected
	if json.Unmarshal(metadata, &expected) != nil {
		t.Fatal("invalid test authority")
	}
	body, err := os.ReadFile(framePath)
	if err != nil {
		t.Fatal("missing test frame")
	}
	prepared, err := Prepare(t.Context(), expected, profile, io.NopCloser(bytes.NewReader(body)))
	if err != nil {
		t.Fatal("valid frame rejected")
	}
	defer prepared.Close()
	seed, err := prepared.SeedRequest()
	if err != nil {
		t.Fatal(err)
	}
	if seed.CodingContractVersion != 2 || seed.TicketID != expected.EvaluationID || seed.CaseID != expected.AttemptID {
		t.Fatal("seed identity drift")
	}
	run, err := prepared.RunRequest("http://workspace.invalid/tool", "http://inference.invalid")
	if err != nil {
		t.Fatal(err)
	}
	visible, _ := json.Marshal(run)
	for _, secret := range []string{"base_task_group_id", "condition", "hidden_grader", "catalog_index", "registration_sha256"} {
		if bytes.Contains(visible, []byte(secret)) {
			t.Fatal("private metadata exposed")
		}
	}
	factory, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{ImageRepository: "example.invalid/authoring", CandidateUID: 10001, CandidateGID: 10001, RequireRootless: true, RequireIsolatedDaemon: true})
	if err != nil {
		t.Fatal(err)
	}
	session, err := prepared.Begin(t.Context(), factory)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	request := codingrunner.ToolRequest{CodingContractVersion: 2, CaseID: expected.AttemptID, ProfileCapabilityID: "hosted-" + expected.AttemptID, CallID: "read-1", Name: "repo.read_file", Arguments: json.RawMessage(`{"path":"app.py"}`)}
	wire, _ := json.Marshal(request)
	response := httptest.NewRecorder()
	session.Handler().ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/tool", bytes.NewReader(wire)))
	if response.Code != 200 || !bytes.Contains(response.Body.Bytes(), []byte("print(1)")) {
		t.Fatal("native workspace projection failed")
	}
	if _, err := prepared.Begin(t.Context(), factory); err == nil {
		t.Fatal("prepared inputs reused")
	}
	if _, err := json.Marshal(prepared); err == nil {
		t.Fatal("private prepared inputs serialized")
	}
	prepared.Close()
	if _, err := prepared.SeedRequest(); err == nil {
		t.Fatal("closed inputs served memory")
	}
	for _, kind := range []string{"digest", "truncated", "task", "deadline", "profile", "grader"} {
		t.Run(kind, func(t *testing.T) {
			changed := bytes.Clone(body)
			authority := expected
			p := profile
			switch kind {
			case "digest":
				changed[4+int(binary.BigEndian.Uint32(changed[:4]))] ^= 1
			case "truncated":
				changed = changed[:len(changed)-1]
			case "task":
				authority.TaskCommitmentSHA256 = strings.Repeat("f", 64)
			case "deadline":
				authority.DeadlineUnix = time.Now().Unix() - 1
			case "profile":
				p.Budgets.ModelInputTokens++
			case "grader":
				n := int(binary.BigEndian.Uint32(changed[:4]))
				var raw map[string]any
				if json.Unmarshal(changed[4:4+n], &raw) != nil {
					t.Fatal("fixture header invalid")
				}
				raw["objects"].([]any)[0].(map[string]any)["role"] = "grader_bundle"
				encoded, _ := json.Marshal(raw)
				encoded = append(encoded, '\n')
				frame := make([]byte, 4)
				binary.BigEndian.PutUint32(frame, uint32(len(encoded)))
				frame = append(frame, encoded...)
				changed = append(frame, changed[4+n:]...)
			}
			if _, err := Prepare(t.Context(), authority, p, io.NopCloser(bytes.NewReader(changed))); err == nil {
				t.Fatal("invalid frame accepted")
			}
		})
	}
	reader, writer := io.Pipe()
	defer writer.Close()
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { _, err := Prepare(ctx, expected, profile, reader); done <- err }()
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancelled read succeeded")
		}
	case <-time.After(time.Second):
		t.Fatal("cancelled reader blocked")
	}
}

func TestProfileRejectsImplicitExecutionAuthority(t *testing.T) {
	if _, err := ProfileDigest(Profile{}); err == nil {
		t.Fatal("empty profile accepted")
	}
	p := fixtureProfile()
	p.ImageDigest = "latest"
	if _, err := ProfileDigest(p); err == nil {
		t.Fatal("floating image accepted")
	}
}
