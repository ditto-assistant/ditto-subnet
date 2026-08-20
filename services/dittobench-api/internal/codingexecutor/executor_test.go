package codingexecutor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

type fakeDocker struct {
	mu               sync.Mutex
	security         []string
	labels           map[string]string
	image            dockerImageInspection
	runs             [][]string
	requests         []supervisorRequest
	active           map[string]fakeContainer
	names            map[string]string
	responseMutate   func(*supervisorResponse)
	responseSymlink  bool
	inspectionMutate func(*dockerContainerInspection)
	createErr        error
	lateCreateDelay  time.Duration
	runErr           error
	removeErr        error
	terminalOOM      bool
	terminalExit     int
}

type fakeContainer struct {
	id      string
	name    string
	args    []string
	control string
	started bool
	oom     bool
	exit    int
}

func newFakeDocker(config Config) *fakeDocker {
	docker := &fakeDocker{
		security: []string{"name=seccomp,profile=default", "name=rootless"},
		labels:   map[string]string{"io.heyditto.dittobench.isolated": "true"},
		image: dockerImageInspection{
			ID: "sha256:" + strings.Repeat("b", 64),
			RepoDigests: []string{
				"registry.invalid/dittobench-coding-supervisor@" + config.Manifest.GraderImageDigest,
			},
			OS: "linux", Architecture: "amd64",
		},
		active: make(map[string]fakeContainer),
		names:  make(map[string]string),
	}
	docker.image.Config.Labels = map[string]string{"io.heyditto.dittobench.coding-supervisor-contract": "1"}
	return docker
}

func (docker *fakeDocker) Output(_ context.Context, args ...string) ([]byte, error) {
	docker.mu.Lock()
	defer docker.mu.Unlock()
	switch {
	case slices.Equal(args, []string{"info", "--format", "{{json .SecurityOptions}}"}):
		return json.Marshal(docker.security)
	case slices.Equal(args, []string{"info", "--format", "{{json .Labels}}"}):
		return json.Marshal(docker.labels)
	case len(args) == 3 && args[0] == "image" && args[1] == "inspect":
		return json.Marshal([]dockerImageInspection{docker.image})
	case len(args) > 1 && args[0] == "create":
		docker.runs = append(docker.runs, append([]string(nil), args...))
		name := flagValue(args, "--name")
		id := fmt.Sprintf("%064x", len(docker.active)+1)
		container := fakeContainer{id: id, name: name, args: append([]string(nil), args...), control: mountSource(args, controlMountPath)}
		if docker.createErr != nil {
			if docker.lateCreateDelay > 0 {
				delay := docker.lateCreateDelay
				go func() {
					time.Sleep(delay)
					docker.mu.Lock()
					docker.active[id] = container
					docker.names[name] = id
					docker.mu.Unlock()
				}()
			}
			return nil, docker.createErr
		}
		docker.active[id] = container
		docker.names[name] = id
		return []byte(id + "\n"), nil
	case len(args) == 3 && args[0] == "container" && args[1] == "inspect":
		if container, ok := docker.fakeContainer(args[2]); ok {
			inspection := inspectionFromArgs(container)
			inspection.Image = docker.image.ID
			if docker.inspectionMutate != nil {
				docker.inspectionMutate(&inspection)
			}
			return json.Marshal([]dockerContainerInspection{inspection})
		}
		return []byte("Error: No such container"), errors.New("container absent")
	case len(args) == 4 && args[0] == "rm" && args[1] == "-f" && args[2] == "-v":
		if docker.removeErr != nil {
			return nil, docker.removeErr
		}
		container, ok := docker.fakeContainer(args[3])
		if ok {
			delete(docker.active, container.id)
			delete(docker.names, container.name)
		}
		return []byte(args[3]), nil
	default:
		return nil, errors.New("unexpected Docker control command")
	}
}

func (docker *fakeDocker) fakeContainer(target string) (fakeContainer, bool) {
	if container, ok := docker.active[target]; ok {
		return container, true
	}
	id, ok := docker.names[target]
	if !ok {
		return fakeContainer{}, false
	}
	container, ok := docker.active[id]
	return container, ok
}

func (docker *fakeDocker) Run(_ context.Context, args ...string) error {
	docker.mu.Lock()
	if len(args) != 3 || args[0] != "start" || args[1] != "--attach" {
		docker.mu.Unlock()
		return errors.New("unexpected Docker runtime command")
	}
	container, ok := docker.fakeContainer(args[2])
	if !ok {
		docker.mu.Unlock()
		return errors.New("container absent")
	}
	control := container.control
	container.started = true
	container.oom = docker.terminalOOM
	container.exit = docker.terminalExit
	docker.active[container.id] = container
	docker.mu.Unlock()

	body, err := os.ReadFile(filepath.Join(control, "request.json"))
	if err != nil {
		return err
	}
	var request supervisorRequest
	if err := json.Unmarshal(body, &request); err != nil {
		return err
	}
	response := supervisorResponse{
		Schema: supervisorResponseSchema, Nonce: request.Nonce, Mode: request.Mode,
		CommandID: request.CommandID, CommandSHA256: request.CommandSHA256,
		ReturnCode: 0, Passed: request.ExpectedTotal, Total: request.ExpectedTotal,
		Completed: true, ProcessTreeDead: true,
	}
	docker.mu.Lock()
	docker.requests = append(docker.requests, request)
	mutate := docker.responseMutate
	responseSymlink := docker.responseSymlink
	runErr := docker.runErr
	docker.mu.Unlock()
	if mutate != nil {
		mutate(&response)
	}
	if runErr != nil {
		return runErr
	}
	responseBody, _ := json.Marshal(response)
	if responseSymlink {
		target := filepath.Join(control, "forged-response.json")
		if err := os.WriteFile(target, responseBody, 0o600); err != nil {
			return err
		}
		return os.Symlink(target, filepath.Join(control, "response.json"))
	}
	return os.WriteFile(filepath.Join(control, "response.json"), responseBody, 0o600)
}

func inspectionFromArgs(container fakeContainer) dockerContainerInspection {
	args := container.args
	var inspection dockerContainerInspection
	inspection.ID = container.id
	inspection.Name = "/" + container.name
	inspection.State.Running = false
	inspection.State.OOMKilled = container.oom
	inspection.State.ExitCode = container.exit
	inspection.Config.Image = imageArgument(args)
	inspection.Config.User = flagValue(args, "--user")
	inspection.Config.Entrypoint = []string{flagValue(args, "--entrypoint")}
	inspection.HostConfig.ReadonlyRootfs = slices.Contains(args, "--read-only")
	inspection.HostConfig.NetworkMode = flagValue(args, "--network")
	inspection.HostConfig.IpcMode = flagValue(args, "--ipc")
	inspection.HostConfig.UTSMode = flagValue(args, "--uts")
	inspection.HostConfig.CapDrop = flagValues(args, "--cap-drop")
	inspection.HostConfig.CapAdd = flagValues(args, "--cap-add")
	inspection.HostConfig.SecurityOpt = flagValues(args, "--security-opt")
	inspection.HostConfig.Memory, _ = strconv.ParseInt(flagValue(args, "--memory"), 10, 64)
	inspection.HostConfig.MemorySwap, _ = strconv.ParseInt(flagValue(args, "--memory-swap"), 10, 64)
	cpu, _ := strconv.ParseFloat(flagValue(args, "--cpus"), 64)
	inspection.HostConfig.NanoCPUs = int64(cpu * 1_000_000_000)
	inspection.HostConfig.PidsLimit, _ = strconv.ParseInt(flagValue(args, "--pids-limit"), 10, 64)
	inspection.HostConfig.LogConfig.Type = flagValue(args, "--log-driver")
	tmpfs := flagValue(args, "--tmpfs")
	if path, options, ok := strings.Cut(tmpfs, ":"); ok {
		inspection.HostConfig.Tmpfs = map[string]string{path: options}
	}
	for _, raw := range flagValues(args, "--mount") {
		mount := dockerMountInspection{Type: "bind"}
		for _, part := range strings.Split(raw, ",") {
			switch {
			case strings.HasPrefix(part, "src="):
				mount.Source = strings.TrimPrefix(part, "src=")
			case strings.HasPrefix(part, "dst="):
				mount.Destination = strings.TrimPrefix(part, "dst=")
			case part == "readonly":
				mount.RW = false
			}
		}
		if !strings.Contains(raw, ",readonly") {
			mount.RW = true
		}
		inspection.Mounts = append(inspection.Mounts, mount)
	}
	return inspection
}

func flagValues(args []string, flag string) []string {
	var values []string
	for index := 0; index+1 < len(args); index++ {
		if args[index] == flag {
			values = append(values, args[index+1])
		}
	}
	return values
}

func imageArgument(args []string) string {
	entrypoint := slices.Index(args, "--entrypoint")
	if entrypoint >= 0 && entrypoint+2 < len(args) {
		return args[entrypoint+2]
	}
	return ""
}

func flagValue(args []string, flag string) string {
	for index := 0; index+1 < len(args); index++ {
		if args[index] == flag {
			return args[index+1]
		}
	}
	return ""
}

func mountSource(args []string, destination string) string {
	for index := 0; index+1 < len(args); index++ {
		if args[index] != "--mount" {
			continue
		}
		parts := strings.Split(args[index+1], ",")
		var source, target string
		for _, part := range parts {
			switch {
			case strings.HasPrefix(part, "src="):
				source = strings.TrimPrefix(part, "src=")
			case strings.HasPrefix(part, "dst="):
				target = strings.TrimPrefix(part, "dst=")
			}
		}
		if target == destination {
			return source
		}
	}
	return ""
}

func hasFlagPair(args []string, flag, value string) bool {
	for index := 0; index+1 < len(args); index++ {
		if args[index] == flag && args[index+1] == value {
			return true
		}
	}
	return false
}

func testConfig(t *testing.T) Config {
	t.Helper()
	limits := codingrunner.DefaultLimits()
	policy := codinggrader.ResourcePolicy{
		CandidateLimits: limits, ProtectedLimits: limits,
		MaxCombinedDiskBytes: 4 << 30, MemoryLimitBytes: 4 << 30, ScratchLimitBytes: 1 << 30,
		PidsLimit: 512, CPUQuotaMillis: 2_000,
	}
	resourceSHA, err := codinggrader.ResourceProfileSHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	groups := make([]codinggrader.TestGroupSpec, 0, 5)
	for index, name := range []string{"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"} {
		groups = append(groups, codinggrader.TestGroupSpec{
			Group: name,
			Command: codingrunner.CommandSpec{
				ID: "test-" + name, Argv: []string{trustedTestDriverName, "grader/" + name + ".json"}, Timeout: time.Minute,
			},
			ExpectedTotal: uint32(index + 1),
		})
	}
	digest := "sha256:" + strings.Repeat("a", 64)
	manifest := codinggrader.Manifest{
		CodingContractVersion: codingrunner.ContractVersion, CaseID: "case-executor-001", VariantID: "variant-v1",
		VisibleBundleSHA256: strings.Repeat("1", 64), BaseTreeSHA256: strings.Repeat("2", 64),
		GraderContractSHA256: codinggrader.GraderContractSHA256(), GraderBundleSHA256: strings.Repeat("3", 64),
		GraderImageDigest: digest, GraderPlatform: "linux/amd64", TestManifestSHA256: strings.Repeat("4", 64),
		ResourceProfileSHA256: resourceSHA, Deadline: time.Now().Add(time.Hour), ExecutionTimeout: 30 * time.Minute,
		ResourcePolicy: policy,
		Build: codinggrader.BuildSpec{Required: true, Command: codingrunner.CommandSpec{
			ID: "build-python", Argv: []string{"python", "-m", "compileall", "src"}, Timeout: time.Minute,
		}},
		TestGroups: groups,
	}
	planSHA, err := codinggrader.GraderPlanSHA256(manifest)
	if err != nil {
		t.Fatal(err)
	}
	manifest.GraderPlanSHA256 = planSHA
	return Config{
		Manifest: manifest, ImageRef: "registry.invalid/dittobench-coding-supervisor@" + digest,
		CandidateUID: 65532, CandidateGID: 65532, RequireRootless: true, RequireIsolatedDaemon: true,
	}
}

func TestPreflightBindsActualDaemonImageAndPlan(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	attestation, err := executor.Preflight(t.Context(), config.Manifest.GraderPlanSHA256)
	if err != nil || attestation.ExecutorInstanceID != executor.InstanceID() || !attestation.NetworkDisabled ||
		!attestation.CandidateMountReadOnly || !attestation.ProtectedMountHidden || !attestation.ProcessGroupsIsolated ||
		attestation.GraderImageDigest != config.Manifest.GraderImageDigest ||
		attestation.GraderPlanSHA256 != config.Manifest.GraderPlanSHA256 {
		t.Fatalf("attestation=%#v err=%v", attestation, err)
	}
}

func TestPreflightRejectsUntrustedDaemonAndImage(t *testing.T) {
	tests := map[string]func(*fakeDocker){
		"not rootless": func(value *fakeDocker) { value.security = []string{"name=seccomp"} },
		"not isolated": func(value *fakeDocker) { value.labels = map[string]string{} },
		"wrong image digest": func(value *fakeDocker) {
			value.image.RepoDigests = []string{"registry.invalid/wrong@sha256:" + strings.Repeat("b", 64)}
		},
		"wrong platform":   func(value *fakeDocker) { value.image.Architecture = "arm64" },
		"invalid image id": func(value *fakeDocker) { value.image.ID = "latest" },
		"baked credential": func(value *fakeDocker) { value.image.Config.Env = []string{"API_TOKEN=secret"} },
		"declared volume":  func(value *fakeDocker) { value.image.Config.Volumes = map[string]any{"/data": struct{}{}} },
		"missing supervisor label": func(value *fakeDocker) {
			value.image.Config.Labels = map[string]string{}
		},
		"certification fixture": func(value *fakeDocker) {
			value.image.Config.Labels["io.heyditto.dittobench.coding-supervisor-fixture"] = "true"
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			config := testConfig(t)
			docker := newFakeDocker(config)
			mutate(docker)
			executor, err := newWithDocker(config, docker)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := executor.Preflight(t.Context(), config.Manifest.GraderPlanSHA256); err == nil {
				t.Fatal("untrusted Docker authority was accepted")
			}
		})
	}
}

func TestPreflightAttestationRequiresObservedContainerPolicy(t *testing.T) {
	tests := map[string]func(*dockerContainerInspection){
		"network": func(value *dockerContainerInspection) { value.HostConfig.NetworkMode = "bridge" },
		"candidate mount": func(value *dockerContainerInspection) {
			for index := range value.Mounts {
				if value.Mounts[index].Destination == workspaceMountPath {
					value.Mounts[index].RW = true
				}
			}
		},
		"memory":     func(value *dockerContainerInspection) { value.HostConfig.Memory++ },
		"privileged": func(value *dockerContainerInspection) { value.HostConfig.Privileged = true },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			config := testConfig(t)
			docker := newFakeDocker(config)
			docker.inspectionMutate = mutate
			executor, _ := newWithDocker(config, docker)
			if _, err := executor.Preflight(t.Context(), config.Manifest.GraderPlanSHA256); err == nil {
				t.Fatal("unobserved container policy was attested")
			}
		})
	}
}

func TestConfigRequiresPinnedFailClosedAuthority(t *testing.T) {
	tests := map[string]func(*Config){
		"floating image":    func(value *Config) { value.ImageRef = "registry.invalid/grader:latest" },
		"option-like image": func(value *Config) { value.ImageRef = "--pull@" + value.Manifest.GraderImageDigest },
		"rootful daemon":    func(value *Config) { value.RequireRootless = false },
		"shared daemon":     func(value *Config) { value.RequireIsolatedDaemon = false },
		"root candidate":    func(value *Config) { value.CandidateUID = 0 },
		"plan drift":        func(value *Config) { value.Manifest.GraderPlanSHA256 = strings.Repeat("f", 64) },
		"resource drift":    func(value *Config) { value.Manifest.ResourcePolicy.MemoryLimitBytes++ },
		"custom supervisor": func(value *Config) { value.SupervisorPath = "/tmp/supervisor" },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			config := testConfig(t)
			mutate(&config)
			if _, err := newWithDocker(config, newFakeDocker(config)); err == nil {
				t.Fatal("invalid coding executor config was accepted")
			}
		})
	}
}

func TestConfigRejectsUntrustedTestCommandEvenWhenPlanIsRebound(t *testing.T) {
	config := testConfig(t)
	config.Manifest.TestGroups[0].Command.Argv = []string{"python3", "-m", "pytest"}
	planSHA, err := codinggrader.GraderPlanSHA256(config.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	config.Manifest.GraderPlanSHA256 = planSHA
	if _, err := newWithDocker(config, newFakeDocker(config)); err == nil ||
		!strings.Contains(err.Error(), "trusted driver") {
		t.Fatalf("untrusted test command error=%v", err)
	}
}

func TestConfigRequiresScratchForTheBoundedWorkspace(t *testing.T) {
	config := testConfig(t)
	config.Manifest.ResourcePolicy.ScratchLimitBytes = uint64(config.Manifest.ResourcePolicy.CandidateLimits.MaxWorkspaceBytes - 1)
	resourceSHA, err := codinggrader.ResourceProfileSHA256(config.Manifest.ResourcePolicy)
	if err != nil {
		t.Fatal(err)
	}
	config.Manifest.ResourceProfileSHA256 = resourceSHA
	planSHA, err := codinggrader.GraderPlanSHA256(config.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	config.Manifest.GraderPlanSHA256 = planSHA
	if _, err := newWithDocker(config, newFakeDocker(config)); err == nil || !strings.Contains(err.Error(), "scratch") {
		t.Fatalf("undersized scratch error=%v", err)
	}
}

func TestAuthoringAndGradingUseHardenedNetworklessContainers(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	workspace := t.TempDir()
	protected := t.TempDir()
	docker.responseMutate = func(response *supervisorResponse) {
		if response.Mode == modeAuthoring {
			response.Stdout = workspace + "/src/app.py\n"
			response.Stderr = "targeted test failed"
		}
	}
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	command := codingrunner.CommandSpec{ID: "visible-tests", Argv: []string{"python", "-m", "pytest", "tests"}, Timeout: time.Minute}
	authoring, err := executor.Execute(t.Context(), workspace, command)
	if err != nil || strings.Contains(authoring.Stdout, workspace) || !strings.Contains(authoring.Stdout, "<workspace>") {
		t.Fatalf("authoring=%#v err=%v", authoring, err)
	}
	group := config.Manifest.TestGroups[2]
	graded, err := executor.Test(t.Context(), workspace, protected, group)
	if err != nil || !graded.Completed || graded.Passed != group.ExpectedTotal || graded.Total != group.ExpectedTotal {
		t.Fatalf("graded=%#v err=%v", graded, err)
	}

	docker.mu.Lock()
	allRuns := append([][]string(nil), docker.runs...)
	requests := append([]supervisorRequest(nil), docker.requests...)
	docker.mu.Unlock()
	var runs [][]string
	for _, args := range allRuns {
		if !strings.HasPrefix(flagValue(args, "--name"), "dittobench-coding-probe-") {
			runs = append(runs, args)
		}
	}
	if len(runs) != 2 || len(requests) != 2 {
		t.Fatalf("runs=%d requests=%d", len(runs), len(requests))
	}
	if requests[0].Schema != supervisorRequestSchema || len(requests[0].Nonce) != 48 ||
		requests[0].CandidateUID != config.CandidateUID || requests[0].CandidateGID != config.CandidateGID ||
		requests[0].TimeoutMilliseconds != command.Timeout.Milliseconds() || requests[1].ExpectedTotal != group.ExpectedTotal {
		t.Fatalf("supervisor requests lost authority: %#v", requests)
	}
	for _, args := range runs {
		for _, pair := range [][2]string{
			{"--network", "none"}, {"--read-only", "--ipc"}, {"--cap-drop", "ALL"},
			{"--security-opt", "no-new-privileges"}, {"--log-driver", "none"},
			{"--platform", "linux/amd64"}, {"--pull", "never"},
		} {
			if pair[0] == "--read-only" {
				if !slices.Contains(args, "--read-only") || !hasFlagPair(args, "--ipc", "none") {
					t.Fatalf("missing read-only/ipc flags: %v", args)
				}
				continue
			}
			if !hasFlagPair(args, pair[0], pair[1]) {
				t.Fatalf("missing %v: %v", pair, args)
			}
		}
		joined := strings.Join(args, " ")
		if strings.Contains(joined, "docker.sock") || slices.Contains(args, "-e") ||
			strings.Contains(joined, "HTTP_PROXY") || strings.Contains(joined, "HTTPS_PROXY") {
			t.Fatalf("sandbox leaked an egress or authority surface: %v", args)
		}
	}
	if !strings.Contains(flagValue(runs[0], "--mount"), "readonly") {
		t.Fatal("authoring source workspace was not read-only")
	}
	graderWorkspaceMount := ""
	for index := 0; index+1 < len(runs[1]); index++ {
		if runs[1][index] == "--mount" && strings.Contains(runs[1][index+1], "dst="+workspaceMountPath) {
			graderWorkspaceMount = runs[1][index+1]
		}
	}
	if !strings.Contains(graderWorkspaceMount, "readonly") || mountSource(runs[1], protectedMountPath) != protected {
		t.Fatalf("grader mounts are not isolated: %v", runs[1])
	}
}

func TestSupervisorReceiptFailsClosedAndContainersAreRemoved(t *testing.T) {
	tests := map[string]func(*supervisorResponse){
		"nonce":        func(value *supervisorResponse) { value.Nonce = "wrong" },
		"command":      func(value *supervisorResponse) { value.CommandSHA256 = strings.Repeat("f", 64) },
		"process tree": func(value *supervisorResponse) { value.ProcessTreeDead = false },
		"incomplete":   func(value *supervisorResponse) { value.Completed = false },
		"count":        func(value *supervisorResponse) { value.Total++ },
		"grader output": func(value *supervisorResponse) {
			value.Stdout = "candidate-controlled"
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			config := testConfig(t)
			docker := newFakeDocker(config)
			docker.responseMutate = mutate
			executor, _ := newWithDocker(config, docker)
			_, err := executor.Test(t.Context(), t.TempDir(), t.TempDir(), config.Manifest.TestGroups[0])
			if err == nil {
				t.Fatal("invalid supervisor receipt was accepted")
			}
			docker.mu.Lock()
			remaining := len(docker.active)
			docker.mu.Unlock()
			if remaining != 0 {
				t.Fatalf("containers remained after failure: %d", remaining)
			}
		})
	}
}

func TestSupervisorResponseSymlinkIsRejected(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	docker.responseSymlink = true
	executor, _ := newWithDocker(config, docker)
	if _, err := executor.Build(t.Context(), t.TempDir(), config.Manifest.Build.Command); err == nil {
		t.Fatal("symlinked supervisor response was accepted")
	}
}

func TestAuthoringOutputIsBoundedBeforeItBecomesModelVisible(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	docker.responseMutate = func(value *supervisorResponse) {
		value.Stdout = strings.Repeat("x", maxModelVisibleCommandOutput+1)
	}
	executor, _ := newWithDocker(config, docker)
	command := codingrunner.CommandSpec{ID: "visible-tests", Argv: []string{"python", "-m", "pytest"}, Timeout: time.Minute}
	if _, err := executor.Execute(t.Context(), t.TempDir(), command); err == nil {
		t.Fatal("oversized authoring output was accepted")
	}
}

func TestTimeoutReceiptIsCandidateResultAndCleanupFailureOverrides(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	docker.responseMutate = func(value *supervisorResponse) {
		value.Completed = false
		value.TimedOut = true
		value.ReturnCode = 124
		value.Passed = 0
	}
	executor, _ := newWithDocker(config, docker)
	result, err := executor.Test(t.Context(), t.TempDir(), t.TempDir(), config.Manifest.TestGroups[0])
	if err != nil || !result.TimedOut || result.Completed {
		t.Fatalf("timeout result=%#v err=%v", result, err)
	}

	docker = newFakeDocker(config)
	docker.terminalOOM = true
	docker.terminalExit = 137
	docker.runErr = errors.New("container exited 137")
	executor, _ = newWithDocker(config, docker)
	oom, err := executor.Test(t.Context(), t.TempDir(), t.TempDir(), config.Manifest.TestGroups[0])
	if err != nil || !oom.Completed || oom.TimedOut || oom.ReturnCode != 137 || oom.Passed != 0 {
		t.Fatalf("OOM result=%#v err=%v", oom, err)
	}

	docker = newFakeDocker(config)
	docker.removeErr = errors.New("daemon cleanup failed")
	executor, _ = newWithDocker(config, docker)
	if _, err := executor.Build(t.Context(), t.TempDir(), config.Manifest.Build.Command); err == nil ||
		!strings.Contains(err.Error(), "remove coding sandbox container") {
		t.Fatalf("cleanup failure did not override success: %v", err)
	}

	docker = newFakeDocker(config)
	docker.runErr = errors.New("container runtime failed")
	executor, _ = newWithDocker(config, docker)
	if _, err := executor.Build(t.Context(), t.TempDir(), config.Manifest.Build.Command); err == nil {
		t.Fatal("container runtime failure was ignored")
	}
	docker.mu.Lock()
	remaining := len(docker.active)
	docker.mu.Unlock()
	if remaining != 0 {
		t.Fatalf("failed container remained after exact cleanup: %d", remaining)
	}

	docker = newFakeDocker(config)
	executor, _ = newWithDocker(config, docker)
	if _, err := executor.Preflight(t.Context(), config.Manifest.GraderPlanSHA256); err != nil {
		t.Fatal(err)
	}
	docker.runErr = errors.New("container runtime failed")
	docker.removeErr = errors.New("daemon cleanup failed")
	_, err = executor.Build(t.Context(), t.TempDir(), config.Manifest.Build.Command)
	if err == nil || !strings.Contains(err.Error(), "container runtime failed") ||
		!strings.Contains(err.Error(), "remove coding sandbox container") {
		t.Fatalf("combined execution/cleanup evidence was lost: %v", err)
	}
}

func TestCreateFailureCleanupCatchesLateDaemonMaterialization(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	docker.createErr = errors.New("create client deadline")
	docker.lateCreateDelay = 250 * time.Millisecond
	executor, _ := newWithDocker(config, docker)
	_, err := executor.Build(t.Context(), t.TempDir(), config.Manifest.Build.Command)
	if err == nil || !strings.Contains(err.Error(), "create coding sandbox container") {
		t.Fatalf("create failure=%v", err)
	}
	docker.mu.Lock()
	remaining := len(docker.active)
	docker.mu.Unlock()
	if remaining != 0 {
		t.Fatalf("late-created container escaped cleanup: %d", remaining)
	}
}

func TestWorkspaceAndProtectedPathsMustBeDistinctRealDirectories(t *testing.T) {
	config := testConfig(t)
	executor, _ := newWithDocker(config, newFakeDocker(config))
	workspace := t.TempDir()
	if _, err := executor.Test(t.Context(), workspace, workspace, config.Manifest.TestGroups[0]); err == nil {
		t.Fatal("overlapping candidate and protected roots were accepted")
	}
	if _, err := executor.Build(t.Context(), filepath.Join(workspace, "missing"), config.Manifest.Build.Command); err == nil {
		t.Fatal("missing workspace was accepted")
	}
	if _, err := executor.Build(t.Context(), string(filepath.Separator), config.Manifest.Build.Command); err == nil {
		t.Fatal("filesystem root was accepted as a coding workspace")
	}
}
