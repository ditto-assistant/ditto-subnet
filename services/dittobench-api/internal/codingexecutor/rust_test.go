package codingexecutor

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
)

func rustFixture(t *testing.T) (workspace, grader, control string, argv []string) {
	t.Helper()
	workspace = t.TempDir()
	grader = t.TempDir()
	control = t.TempDir()
	if err := os.Mkdir(filepath.Join(workspace, "src"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workspace, "src/lib.rs"), []byte("public synthetic implementation"), 0600); err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"schema":"dittobench-coding-rust-authority-v1","group":"hidden","files":["src/lib.rs"]}`)
	if err := os.WriteFile(filepath.Join(grader, "authority.json"), body, 0600); err != nil {
		t.Fatal(err)
	}
	argv = []string{trustedTestDriverName, "--group", "hidden", "--authority", "authority.json", "--authority-sha256", hex.EncodeToString(sha256Sum(body))}
	return
}
func TestRustFreezeInputsComeFromApprovedPathsAndExactFrozenBytes(t *testing.T) {
	w, g, c, args := rustFixture(t)
	if err := os.WriteFile(filepath.Join(w, "unlisted.rs"), []byte("never compiler input"), 0600); err != nil {
		t.Fatal(err)
	}
	binding, err := prepareRustInputs(w, g, c, args, "sha256:"+strings.Repeat("d", 64))
	if err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(filepath.Join(c, "rust-inputs.json"))
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(sha256Sum(body)) != binding.InputsSHA256 || strings.Contains(string(body), "unlisted") {
		t.Fatal("freeze binding changed scope")
	}
	var value struct {
		Files []struct{ Path, SHA256 string }
	}
	if json.Unmarshal(body, &value) != nil || len(value.Files) != 1 || value.Files[0].Path != "src/lib.rs" || value.Files[0].SHA256 != hex.EncodeToString(sha256Sum([]byte("public synthetic implementation"))) {
		t.Fatal("input content was not bound")
	}
	if _, err := prepareRustInputs(w, g, c, args, "sha256:"+strings.Repeat("d", 64)); err == nil {
		t.Fatal("input control file overwritten")
	}
}
func TestRustFreezeRejectsWrongAuthorityAndSourceLinks(t *testing.T) {
	for _, scenario := range []string{"digest", "symlink", "hardlink", "source-mode", "duplicate", "extra-file", "unsafe-path"} {
		t.Run(scenario, func(t *testing.T) {
			w, g, c, args := rustFixture(t)
			source := filepath.Join(w, "src/lib.rs")
			switch scenario {
			case "digest":
				args[6] = strings.Repeat("a", 64)
			case "symlink":
				if err := os.Rename(source, source+".saved"); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink("lib.rs.saved", source); err != nil {
					t.Fatal(err)
				}
			case "hardlink":
				if err := os.Link(source, source+".linked"); err != nil {
					t.Fatal(err)
				}
			case "source-mode":
				if err := os.Chmod(source, 0666); err != nil {
					t.Fatal(err)
				}
			default:
				files := []string{"src/lib.rs", "src/lib.rs"}
				if scenario == "extra-file" {
					files = []string{"src/lib.rs", "src/absent.rs"}
				}
				if scenario == "unsafe-path" {
					files = []string{"src/lib.rs", "src/../secret.rs"}
				}
				body, _ := json.Marshal(map[string]any{"schema": "dittobench-coding-rust-authority-v1", "group": "hidden", "files": files})
				if err := os.WriteFile(filepath.Join(g, "authority.json"), body, 0600); err != nil {
					t.Fatal(err)
				}
				args[6] = hex.EncodeToString(sha256Sum(body))
			}
			if _, err := prepareRustInputs(w, g, c, args, "sha256:"+strings.Repeat("d", 64)); err == nil {
				t.Fatal("invalid freeze accepted")
			}
		})
	}
}
func TestRustScratchSplitsExistingBudgetAndRejectsExtraMounts(t *testing.T) {
	executor := &Executor{driverProfile: rustDriverProfile, config: Config{Manifest: codinggrader.Manifest{ResourcePolicy: codinggrader.ResourcePolicy{ScratchLimitBytes: 512 << 20}}}}
	if executor.temporaryBytes() != 384<<20 {
		t.Fatal("scratch budget grew")
	}
	valid := map[string]string{"/tmp": "rw,noexec,nosuid,nodev,size=402653184", "/out": "rw,noexec,nosuid,nodev,size=134217728,uid=10001,gid=10001,mode=0700"}
	if !executor.scratchMatches(valid) {
		t.Fatal("fixed scratch rejected")
	}
	valid["/other"] = "rw"
	if executor.scratchMatches(valid) {
		t.Fatal("extra scratch accepted")
	}
	delete(valid, "/other")
	valid["/out"] += ",exec"
	if executor.scratchMatches(valid) {
		t.Fatal("executable output accepted")
	}
}
func TestRustReportRequiresMatchingRuntimeProof(t *testing.T) {
	sha := strings.Repeat("a", 64)
	request := supervisorRequest{Mode: modeTest, ExpectedTotal: 2, Rust: &rustInputBinding{AuthoritySHA256: sha, InputsSHA256: sha, ImageSHA256: sha}}
	proof := &codinggrader.RuntimeEvidence{Schema: "dittobench-coding-rust-runtime-v1", AuthoritySHA256: sha, InputsSHA256: sha, ImageSHA256: sha, ProgramSHA256: sha, CompilerSHA256: sha, BridgeLibrarySHA256: sha, Outcome: "evaluated", BuildSHA256: sha, ArtifactSHA256: sha}
	if validateRuntimeBinding(proof, request, true, 2, 0) != nil {
		t.Fatal("valid report rejected")
	}
	if validateRuntimeBinding(nil, request, true, 2, 0) == nil {
		t.Fatal("missing proof accepted")
	}
	if validateRuntimeBinding(proof, request, false, 2, 0) == nil {
		t.Fatal("incomplete report accepted")
	}
	if validateRuntimeBinding(proof, request, true, 2, 70) == nil {
		t.Fatal("driver failure accepted")
	}
	proof.InputsSHA256 = strings.Repeat("b", 64)
	if validateRuntimeBinding(proof, request, true, 2, 0) == nil {
		t.Fatal("cross-freeze evidence accepted")
	}
}

func TestRustExecutorCarriesFreezeBindingIntoPrivateResultAndCleansContainer(t *testing.T) {
	w, g, _, args := rustFixture(t)
	config := testConfig(t)
	config.CandidateUID = 10001
	config.CandidateGID = 10001
	for index := range config.Manifest.TestGroups {
		config.Manifest.TestGroups[index].Command.Argv = append([]string(nil), args...)
	}
	plan, err := codinggrader.GraderPlanSHA256(config.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	config.Manifest.GraderPlanSHA256 = plan
	docker := newFakeDocker(config)
	docker.image.Config.Labels["io.heyditto.dittobench.coding-test-driver-profile"] = rustDriverProfile
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	result, err := executor.Test(t.Context(), w, g, config.Manifest.TestGroups[0])
	if err != nil {
		t.Fatal(err)
	}
	if result.Runtime.Validate() != nil || result.Runtime.AuthoritySHA256 != args[6] || result.Runtime.ImageSHA256 != strings.TrimPrefix(config.Manifest.GraderImageDigest, "sha256:") {
		t.Fatal("runtime proof lost supervisor binding")
	}
	if len(docker.active) != 0 {
		t.Fatal("Rust container remained after report")
	}
	docker.terminalOOM = true
	if _, err := executor.Test(t.Context(), w, g, config.Manifest.TestGroups[0]); err == nil {
		t.Fatal("Rust driver OOM synthesized a completed report")
	}
	if len(docker.active) != 0 {
		t.Fatal("Rust OOM container remained")
	}
}
