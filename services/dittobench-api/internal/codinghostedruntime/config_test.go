package codinghostedruntime

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedinput"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedworker"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func privateTemp(t *testing.T) string {
	t.Helper()
	// Short Unix socket names; t.TempDir may exceed sockaddr_un's bound.
	root, err := os.MkdirTemp("", "hosted-runtime-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	return root
}

func write(t *testing.T, path string, body []byte) {
	t.Helper()
	if err := os.WriteFile(path, body, 0600); err != nil {
		t.Fatal(err)
	}
}

func encode(t *testing.T, value any) []byte {
	t.Helper()
	body, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func canonical(t *testing.T, value any) []byte {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(encode(t, value)))
	decoder.UseNumber()
	var decoded any
	if decoder.Decode(&decoded) != nil {
		t.Fatal("fixture encoding")
	}
	return append(encode(t, decoded), '\n')
}

func fixture(t *testing.T) (configWire, string) {
	t.Helper()
	root := privateTemp(t)
	limits := codingrunner.DefaultLimits()
	limits.MaxBundleBytes = 4 << 20
	limits.MaxWorkspaceBytes = 16 << 20
	limits.MaxFileBytes = 4 << 20
	limits.MaxPatchBytes = 1024
	policy := codinggrader.ResourcePolicy{CandidateLimits: limits, ProtectedLimits: limits, MaxCombinedDiskBytes: 1 << 30, MemoryLimitBytes: 1 << 30, ScratchLimitBytes: 512 << 20, PidsLimit: 256, CPUQuotaMillis: 2000}
	p := codinghostedinput.Profile{Schema: "dittobench-coding-hosted-authoring-profile-v2", ImageDigest: "sha256:" + strings.Repeat("a", 64), ResourcePolicy: policy, Budgets: codingcontract.Budgets{ModelInputTokens: 10000, ModelOutputTokens: 1000, WorkspaceToolCalls: limits.MaxToolCalls, WallTimeSeconds: 600}}
	profileSHA, err := codinghostedinput.ProfileDigest(p)
	if err != nil {
		t.Fatal(err)
	}
	cmd := func(id string) codingrunner.CommandSpec {
		return codingrunner.CommandSpec{ID: id, Argv: []string{"dittobench-test-driver", id}, Timeout: time.Minute}
	}
	g := codinghostedworker.GradingProfile{Schema: "dittobench-coding-hosted-grading-profile-v2", ImageDigest: p.ImageDigest, GraderContractSHA256: codinggrader.HostedGraderContractSHA256(), GraderBundleSHA256: strings.Repeat("b", 64), TestManifestSHA256: strings.Repeat("c", 64), ResourcePolicy: policy, Build: codinggrader.BuildSpec{Command: cmd("build")}, TestGroups: []codinggrader.TestGroupSpec{{Group: "hidden", Command: cmd("hidden"), ExpectedTotal: 1}, {Group: "visible", Command: cmd("visible"), ExpectedTotal: 1}}, ExecutionTimeout: time.Minute}
	if g.Validate() != nil {
		t.Fatal("bad grading fixture")
	}
	grading := canonical(t, g)
	on, off := true, false
	expected := codinghostedinput.Expected{EvaluationID: "10000000-0000-4000-8000-000000000001", AttemptID: "20000000-0000-4000-8000-000000000002", WorkerID: "30000000-0000-4000-8000-000000000003", AssignmentSHA256: strings.Repeat("1", 64), RegistrationSHA256: strings.Repeat("2", 64), ExecutionProfileSHA256: profileSHA, TaskCommitmentSHA256: strings.Repeat("3", 64), PrivateReleaseSHA256: strings.Repeat("4", 64), CorpusReleaseID: "synthetic-v2", DeadlineUnix: time.Now().Add(20 * time.Minute).Unix(), MaxPatchBytes: limits.MaxPatchBytes}
	agent := "40000000-0000-4000-8000-000000000004"
	wire := configWire{Schema: "dittobench-coding-hosted-runtime-v2", ShadowOnly: &on, WeightEligible: &off, Expected: expected,
		Harness:              codingharness.HostedBinding{EvaluationID: expected.EvaluationID, AttemptID: expected.AttemptID, WorkerID: expected.WorkerID, AssignmentSHA256: expected.AssignmentSHA256, AgentID: agent, AgentArtifactSHA256: strings.Repeat("5", 64), ProfileCapabilityID: "hosted-" + expected.AttemptID, Deadline: time.Unix(expected.DeadlineUnix, 0).UTC(), ScreenedImageSHA256: strings.Repeat("6", 64), ScreenedImageID: "sha256:" + strings.Repeat("7", 64), ScreenedImageRef: "ditto-screen/" + agent + ":latest", ScreenedImageSize: 1, ScreeningPolicyVersion: 12, ImageURL: "https://storage.invalid/image?signature=private-marker", ImageExpiresAt: time.Now().Add(time.Minute).UTC()},
		AuthoringProfileFile: filepath.Join(root, "authoring.json"), GradingProfileFile: filepath.Join(root, "grading.json"), GradingProfileSHA256: fmt.Sprintf("%x", sha256.Sum256(grading)),
		ControlSocket: filepath.Join(root, "control.sock"), ControlTokenFile: filepath.Join(root, "token"), PythonExecutable: "/approved/python", PostgresEnvironmentFile: filepath.Join(root, "postgres.json"),
		StateRoot: filepath.Join(root, "state"), DockerExecutable: "/approved/docker", DockerSocket: filepath.Join(root, "docker.sock"), RouterListen: "172.21.0.1:19010", EgressNetwork: "coding-restricted", EgressProxy: "http://172.21.0.2:3128", ExecutorRepository: "example.invalid/coding", CandidateUID: 10001, CandidateGID: 10001}
	if os.Mkdir(wire.StateRoot, 0700) != nil {
		t.Fatal("state fixture")
	}
	write(t, wire.AuthoringProfileFile, encode(t, p))
	write(t, wire.GradingProfileFile, grading)
	write(t, wire.ControlTokenFile, bytes.Repeat([]byte{1}, 32))
	write(t, wire.PostgresEnvironmentFile, encode(t, []string{"POSTGRES_HOST=synthetic.invalid", "POSTGRES_PORT=5432", "POSTGRES_USER=synthetic", "POSTGRES_PASSWORD=private-marker", "POSTGRES_DB=synthetic"}))
	for _, path := range []string{wire.ControlSocket, wire.DockerSocket} {
		listener, err := net.Listen("unix", path)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = listener.Close() })
		if os.Chmod(path, 0600) != nil {
			t.Fatal("socket mode")
		}
	}
	return wire, filepath.Join(root, "runtime.json")
}

func writeConfig(t *testing.T, path string, wire configWire) {
	t.Helper()
	// Explicit private fixture serialization, not a production logging escape.
	type harnessWire codingharness.HostedBinding
	type plain configWire
	body := encode(t, struct {
		plain
		Harness harnessWire `json:"harness"`
	}{plain(wire), harnessWire(wire.Harness)})
	write(t, path, body)
}

func testExecutable(path string) bool {
	return path == "/approved/python" || path == "/approved/docker"
}

func TestLoadConfigBindsProfilesAndFixedSandboxPolicy(t *testing.T) {
	wire, path := fixture(t)
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, testExecutable)
	if err != nil {
		t.Fatal(err)
	}
	if !config.docker.RequireRootless || !config.docker.RequireIsolatedDaemon || !config.docker.Harden || config.docker.AllowPrivate || config.docker.GitHubTokenFile != "" || config.docker.OpenRouterShimCABundleHostPath != "" || config.docker.CPULimit != "2.000" || config.publicBase != "http://host.docker.internal:19010" {
		t.Fatal("sandbox confinement drift")
	}
	for _, value := range []any{wire, config} {
		if _, err := json.Marshal(value); err == nil {
			t.Fatal("private configuration serialized")
		}
		if strings.Contains(fmt.Sprintf("%v %#v %+v", value, value, value), "private-marker") {
			t.Fatal("private configuration logged")
		}
	}
	if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
		t.Fatal("validation consumed attempt")
	}
	// Unknown fields stay non-authoritative; duplicate and null known fields do not.
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	write(t, path, append([]byte(`{"future_field":true,`), body[1:]...))
	if _, err := loadConfigChecked(path, testExecutable); err != nil {
		t.Fatal("unknown field rejected")
	}
}

func TestConfigRejectsDriftBeforeConsumingAttempt(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*configWire)
	}{
		{"activation", func(w *configWire) { on := true; w.WeightEligible = &on }},
		{"missing_gate", func(w *configWire) { w.ShadowOnly = nil }},
		{"worker", func(w *configWire) { w.Harness.WorkerID = "40000000-0000-4000-8000-000000000004" }},
		{"deadline", func(w *configWire) { w.Expected.DeadlineUnix-- }},
		{"expired", func(w *configWire) { w.Expected.DeadlineUnix = time.Now().Add(-time.Minute).Unix() }},
		{"profile", func(w *configWire) { w.Expected.ExecutionProfileSHA256 = strings.Repeat("f", 64) }},
		{"grading", func(w *configWire) { w.GradingProfileSHA256 = strings.Repeat("f", 64) }},
		{"patch_limit", func(w *configWire) { w.Expected.MaxPatchBytes-- }},
		{"task_index", func(w *configWire) { w.Expected.CatalogIndex = 250 }},
		{"image", func(w *configWire) { w.Harness.ScreenedImageID = "latest" }},
		{"capability", func(w *configWire) { w.Harness.ProfileCapabilityID = "wrong" }},
		{"wildcard", func(w *configWire) { w.RouterListen = "0.0.0.0:19010" }},
		{"loopback", func(w *configWire) { w.RouterListen = "127.0.0.1:19010" }},
		{"remote_daemon", func(w *configWire) { w.DockerSocket = "tcp://remote:2375" }},
		{"proxy_credentials", func(w *configWire) { w.EgressProxy = "http://secret@172.21.0.2:3128" }},
		{"proxy_public", func(w *configWire) { w.EgressProxy = "http://8.8.8.8:3128" }},
		{"proxy_port", func(w *configWire) { w.EgressProxy = "http://172.21.0.2:99999" }},
		{"unconfined", func(w *configWire) { w.SeccompProfile = "unconfined" }},
		{"executable", func(w *configWire) { w.PythonExecutable = "/unapproved/python" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			wire, path := fixture(t)
			tc.change(&wire)
			writeConfig(t, path, wire)
			if _, err := loadConfigChecked(path, testExecutable); err != ErrConfig {
				t.Fatal("invalid config accepted")
			}
			if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
				t.Fatal("invalid config consumed attempt")
			}
		})
	}
}

func TestConfigRejectsMalformedProfilesSecretsAndJSON(t *testing.T) {
	for _, name := range []string{"duplicate", "null", "missing_catalog_index", "null_catalog_index", "missing_model_budget", "grading_count", "grading_driver", "grading_noncanonical", "token_zero", "postgres_extra", "permissive_docker_socket"} {
		t.Run(name, func(t *testing.T) {
			wire, path := fixture(t)
			writeConfig(t, path, wire)
			switch name {
			case "missing_catalog_index", "null_catalog_index":
				body, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				var object map[string]any
				if json.Unmarshal(body, &object) != nil {
					t.Fatal("fixture JSON")
				}
				expected := object["expected"].(map[string]any)
				if name == "missing_catalog_index" {
					delete(expected, "catalog_index")
				} else {
					expected["catalog_index"] = nil
				}
				write(t, path, encode(t, object))
			case "duplicate", "null":
				body, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				if name == "duplicate" {
					body = append([]byte(`{"schema":"duplicate",`), body[1:]...)
				} else {
					body = []byte(`{"schema":null}`)
				}
				write(t, path, body)
			case "token_zero":
				write(t, wire.ControlTokenFile, make([]byte, 32))
			case "postgres_extra":
				write(t, wire.PostgresEnvironmentFile, []byte(`["PYTHONPATH=/untrusted"]`))
			case "permissive_docker_socket":
				if os.Chmod(wire.DockerSocket, 0666) != nil {
					t.Fatal("chmod")
				}
			case "missing_model_budget":
				var p codinghostedinput.Profile
				body, _ := os.ReadFile(wire.AuthoringProfileFile)
				if json.Unmarshal(body, &p) != nil {
					t.Fatal("profile")
				}
				p.Budgets.ModelInputTokens = 0
				wire.Expected.ExecutionProfileSHA256, _ = codinghostedinput.ProfileDigest(p)
				write(t, wire.AuthoringProfileFile, encode(t, p))
				writeConfig(t, path, wire)
			default:
				var p codinghostedworker.GradingProfile
				body, _ := os.ReadFile(wire.GradingProfileFile)
				if json.Unmarshal(body, &p) != nil {
					t.Fatal("profile")
				}
				if name == "grading_count" {
					p.TestGroups[0].ExpectedTotal = 0
				}
				if name == "grading_driver" {
					p.TestGroups[0].Command.Argv = []string{"echo", "PASS"}
				}
				body = canonical(t, p)
				if name == "grading_noncanonical" {
					body = append([]byte(" "), body...)
				}
				wire.GradingProfileSHA256 = fmt.Sprintf("%x", sha256.Sum256(body))
				write(t, wire.GradingProfileFile, body)
				writeConfig(t, path, wire)
			}
			if _, err := loadConfigChecked(path, testExecutable); err != ErrConfig {
				t.Fatal("unsafe input accepted")
			}
		})
	}
}

func TestPrivateFilesRejectLinksDevicesAndPermissions(t *testing.T) {
	for _, name := range []string{"symlink", "parent_symlink", "hardlink", "fifo", "mode", "parent_mode", "oversize", "empty", "directory"} {
		t.Run(name, func(t *testing.T) {
			root := privateTemp(t)
			path := filepath.Join(root, "input")
			write(t, path, []byte("private-marker"))
			switch name {
			case "symlink":
				link := filepath.Join(root, "link")
				if os.Symlink(path, link) != nil {
					t.Fatal("symlink")
				}
				path = link
			case "parent_symlink":
				link := filepath.Join(root, "link")
				if os.Symlink(root, link) != nil {
					t.Fatal("symlink")
				}
				path = filepath.Join(link, "input")
			case "hardlink":
				if os.Link(path, filepath.Join(root, "link")) != nil {
					t.Fatal("hardlink")
				}
			case "fifo":
				path = filepath.Join(root, "fifo")
				if syscall.Mkfifo(path, 0600) != nil {
					t.Fatal("fifo")
				}
			case "mode":
				if os.Chmod(path, 0644) != nil {
					t.Fatal("chmod")
				}
			case "parent_mode":
				if os.Chmod(root, 0755) != nil {
					t.Fatal("chmod")
				}
			case "oversize":
				write(t, path, make([]byte, 33))
			case "empty":
				write(t, path, nil)
			case "directory":
				path = root
			}
			if _, err := readPrivate(path, 32); err != ErrConfig {
				t.Fatal("unsafe private file accepted")
			}
		})
	}
}

func TestExecutableRejectsWritableAncestry(t *testing.T) {
	root := privateTemp(t)
	path := filepath.Join(root, "docker")
	write(t, path, []byte("synthetic"))
	if os.Chmod(path, 0700) != nil {
		t.Fatal("chmod")
	}
	if protectedExecutable(path) || protectedExecutable("docker") {
		t.Fatal("unprotected executable accepted")
	}
}

func TestPrivateDirectoryRejectsRenameableSharedAncestor(t *testing.T) {
	root := privateTemp(t)
	shared := filepath.Join(root, "shared")
	leaf := filepath.Join(shared, "private")
	if os.Mkdir(shared, 0700) != nil || os.Chmod(shared, 0777) != nil || os.Mkdir(leaf, 0700) != nil {
		t.Fatal("fixture directories")
	}
	if privateDirectory(leaf) {
		t.Fatal("renameable private directory accepted")
	}
}
