package codingexecutor

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

// This certification is opt-in because it requires a dedicated rootless Docker
// daemon carrying the isolated ownership label and a registry-addressed image
// built from Dockerfile.coding-supervisor. It never falls back to a host/rootful
// Docker socket.
func TestDockerExecutorIntegration(t *testing.T) {
	imageRef := strings.TrimSpace(os.Getenv("DITTOBENCH_CODING_EXECUTOR_IMAGE"))
	if imageRef == "" {
		t.Skip("DITTOBENCH_CODING_EXECUTOR_IMAGE is not configured")
	}
	at := strings.LastIndex(imageRef, "@sha256:")
	if at <= 0 {
		t.Fatal("integration image must be registry-addressed by sha256 digest")
	}
	config := testConfig(t)
	config.ImageRef = imageRef
	config.AllowCertificationImage = true
	config.Manifest.GraderImageDigest = imageRef[at+1:]
	planSHA, err := codinggrader.GraderPlanSHA256(config.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	config.Manifest.GraderPlanSHA256 = planSHA
	executor, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := executor.Preflight(t.Context(), planSHA); err != nil {
		t.Fatal(err)
	}
	workspace, protected := t.TempDir(), t.TempDir()
	authoring, err := executor.Execute(t.Context(), workspace, codingrunner.CommandSpec{
		ID: "integration-write", Argv: []string{"fixture-command", "write", "integration.txt", "sandbox-ok"}, Timeout: 10 * time.Second,
	})
	body, readErr := os.ReadFile(filepath.Join(workspace, "integration.txt"))
	if err != nil || authoring.ReturnCode != 0 || !authoring.WorkspaceMutated ||
		!errors.Is(readErr, os.ErrNotExist) || len(body) != 0 {
		t.Fatalf("integration authoring=%#v err=%v", authoring, err)
	}
	group := codinggrader.TestGroupSpec{
		Group: "hidden", ExpectedTotal: 1,
		Command: codingrunner.CommandSpec{
			ID: "integration-hidden", Argv: []string{trustedTestDriverName}, Timeout: 10 * time.Second,
		},
	}
	graded, err := executor.Test(t.Context(), workspace, protected, group)
	if err != nil || !graded.Completed || graded.ReturnCode != 0 || graded.Passed != 1 || graded.Total != 1 {
		t.Fatalf("integration grade=%#v err=%v", graded, err)
	}
}
