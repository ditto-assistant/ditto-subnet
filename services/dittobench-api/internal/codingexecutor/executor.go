package codingexecutor

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"golang.org/x/sys/unix"
)

const maxModelVisibleCommandOutput = 24 << 10

// Executor is a shadow-only adapter for both authoring commands and pristine
// grading. Each command receives a fresh, exact-name networkless container.
type Executor struct {
	config     Config
	docker     dockerCLI
	instanceID string
	imageID    string

	preflightMu sync.Mutex
	preflightOK bool
}

var _ codingrunner.CommandExecutor = (*Executor)(nil)
var _ codinggrader.Executor = (*Executor)(nil)

// New returns a fail-closed Docker executor. No Docker call occurs until
// Preflight or the first command.
func New(config Config) (*Executor, error) {
	if config.hosted {
		return nil, errors.New("hosted grading requires its explicit constructor")
	}
	return newWithDocker(config, execDocker{})
}

func newWithDocker(config Config, docker dockerCLI) (*Executor, error) {
	config = normalizeConfig(config)
	if docker == nil {
		return nil, errors.New("coding Docker client is unavailable")
	}
	if err := config.validate(); err != nil {
		return nil, err
	}
	instanceID, err := randomHex(16)
	if err != nil {
		return nil, err
	}
	return &Executor{config: config, docker: docker, instanceID: "coding-executor-" + instanceID}, nil
}

func randomHex(size int) (string, error) {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		return "", fmt.Errorf("coding executor identity: %w", err)
	}
	return hex.EncodeToString(value), nil
}

func (executor *Executor) ensurePreflight(ctx context.Context) error {
	executor.preflightMu.Lock()
	defer executor.preflightMu.Unlock()
	if executor.preflightOK {
		return nil
	}
	if err := executor.preflightDocker(ctx); err != nil {
		return err
	}
	executor.preflightOK = true
	return nil
}

func (executor *Executor) probeContainerPolicy(ctx context.Context) (returnedErr error) {
	workspace, err := os.MkdirTemp("", "dittobench-coding-probe-workspace-")
	if err != nil {
		return err
	}
	protected, err := os.MkdirTemp("", "dittobench-coding-probe-grader-")
	if err != nil {
		_ = os.RemoveAll(workspace)
		return err
	}
	control, err := os.MkdirTemp("", "dittobench-coding-probe-control-")
	if err != nil {
		_ = os.RemoveAll(workspace)
		_ = os.RemoveAll(protected)
		return err
	}
	defer func() {
		for _, path := range []string{control, protected, workspace} {
			if removeErr := removeControlDirectory(path); removeErr != nil {
				returnedErr = errors.Join(returnedErr, fmt.Errorf("remove coding policy probe: %w", removeErr))
			}
		}
	}()
	nonce, err := randomHex(12)
	if err != nil {
		return err
	}
	name := "dittobench-coding-probe-" + nonce
	cleanupTarget, cleanupUncertain := name, true
	defer func() {
		if cleanupErr := executor.cleanupContainer(cleanupTarget, cleanupUncertain); cleanupErr != nil {
			returnedErr = errors.Join(returnedErr, cleanupErr)
		}
	}()
	containerID, err := executor.createContainer(ctx, name, modeTest, workspace, protected, control)
	if err != nil {
		return err
	}
	cleanupTarget, cleanupUncertain = containerID, false
	return executor.inspectContainerPolicy(ctx, containerID, modeTest, workspace, protected, control)
}

// Preflight verifies the daemon and pinned image before grader bytes are read.
// The grader independently compares every returned identity with its manifest.
func (executor *Executor) Preflight(
	ctx context.Context,
	expectedPlanSHA256 string,
) (codinggrader.ExecutorAttestation, error) {
	if ctx == nil || executor == nil || executor.config.AuthoringOnly {
		return codinggrader.ExecutorAttestation{}, errors.New("coding executor preflight context is required")
	}
	if executor.config.hosted && expectedPlanSHA256 != executor.config.Manifest.GraderPlanSHA256 {
		return codinggrader.ExecutorAttestation{}, errors.New("hosted executor plan does not match")
	}
	if err := executor.ensurePreflight(ctx); err != nil {
		return codinggrader.ExecutorAttestation{}, err
	}
	manifest := executor.config.Manifest
	return codinggrader.ExecutorAttestation{
		ExecutorInstanceID: executor.instanceID, GraderImageDigest: manifest.GraderImageDigest,
		GraderPlatform: manifest.GraderPlatform, GraderContractSHA256: manifest.GraderContractSHA256,
		GraderPlanSHA256: manifest.GraderPlanSHA256, ResourceProfileSHA256: manifest.ResourceProfileSHA256,
		NetworkDisabled: true, CandidateMountReadOnly: true, ProtectedMountHidden: true, ProcessGroupsIsolated: true,
	}, nil
}

// Execute runs one authoring command from a writable scratch copy and no grader
// mount. The coding runner attributes the supervisor's mutation signal.
func (executor *Executor) Execute(
	ctx context.Context,
	workspace string,
	command codingrunner.CommandSpec,
) (codingrunner.CommandResult, error) {
	if executor != nil && executor.config.hosted {
		return codingrunner.CommandResult{}, errors.New("hosted grader cannot run authoring commands")
	}
	result, err := executor.execute(ctx, modeAuthoring, workspace, "", command, 0)
	if err != nil {
		return codingrunner.CommandResult{}, err
	}
	return codingrunner.CommandResult{
		ReturnCode:       result.response.ReturnCode,
		Stdout:           safeDiagnostic(result.response.Stdout, workspace),
		Stderr:           safeDiagnostic(result.response.Stderr, workspace),
		TimedOut:         result.response.TimedOut,
		WorkspaceMutated: result.response.WorkspaceMutated,
		Duration:         result.duration,
	}, nil
}

// Build runs one exact build command against the read-only replayed candidate.
func (executor *Executor) Build(
	ctx context.Context,
	workspace string,
	command codingrunner.CommandSpec,
) (codinggrader.BuildRun, error) {
	if executor != nil && executor.config.hosted &&
		(!executor.config.Manifest.Build.Required || !sameHostedCommand(command, executor.config.Manifest.Build.Command)) {
		return codinggrader.BuildRun{}, errors.New("hosted build command does not match")
	}
	if executor == nil || executor.config.AuthoringOnly {
		return codinggrader.BuildRun{}, errors.New("coding executor is not scoped to grading")
	}
	result, err := executor.execute(ctx, modeBuild, workspace, "", command, 0)
	if err != nil {
		return codinggrader.BuildRun{}, err
	}
	return codinggrader.BuildRun{
		CommandID: result.response.CommandID, CommandSHA256: result.response.CommandSHA256,
		ExecutorInstanceID: executor.instanceID, ReturnCode: result.response.ReturnCode,
		Completed: result.response.Completed, TimedOut: result.response.TimedOut,
	}, nil
}

// Test runs one exact test group after freeze. Protected material is mounted
// read-only only for the trusted supervisor; its receipt exposes no output.
func (executor *Executor) Test(
	ctx context.Context,
	workspace string,
	protectedGrader string,
	group codinggrader.TestGroupSpec,
) (codinggrader.TestRun, error) {
	if executor != nil && executor.config.hosted && !executor.hostedTestAllowed(group) {
		return codinggrader.TestRun{}, errors.New("hosted test command does not match")
	}
	if executor == nil || executor.config.AuthoringOnly {
		return codinggrader.TestRun{}, errors.New("coding executor is not scoped to grading")
	}
	result, err := executor.execute(ctx, modeTest, workspace, protectedGrader, group.Command, group.ExpectedTotal)
	if err != nil {
		return codinggrader.TestRun{}, err
	}
	return codinggrader.TestRun{
		CommandID: result.response.CommandID, CommandSHA256: result.response.CommandSHA256,
		ExecutorInstanceID: executor.instanceID, ReturnCode: result.response.ReturnCode,
		Passed: result.response.Passed, Total: result.response.Total,
		Completed: result.response.Completed, TimedOut: result.response.TimedOut,
	}, nil
}

func (executor *Executor) execute(
	ctx context.Context,
	mode executionMode,
	workspace string,
	protected string,
	command codingrunner.CommandSpec,
	expectedTotal uint32,
) (result executionResult, returnedErr error) {
	if ctx == nil {
		return result, errors.New("coding execution context is required")
	}
	if err := executor.ensurePreflight(ctx); err != nil {
		return result, err
	}
	commandSHA, err := commandDigest(command)
	if err != nil {
		return result, err
	}
	workspace, err = resolveWorkspace(workspace)
	if err != nil {
		return result, err
	}
	if mode == modeTest {
		protected, err = resolveWorkspace(protected)
		if err != nil || pathsOverlap(workspace, protected) {
			return result, errors.New("coding protected grader path is invalid or overlaps the candidate")
		}
	} else if protected != "" {
		return result, errors.New("coding protected grader is available outside a test phase")
	}

	control, err := os.MkdirTemp("", "dittobench-coding-control-")
	if err != nil {
		return result, fmt.Errorf("create coding control directory: %w", err)
	}
	if err := os.Chmod(control, 0o700); err != nil {
		_ = os.RemoveAll(control)
		return result, err
	}
	defer func() {
		if err := removeControlDirectory(control); err != nil {
			returnedErr = errors.Join(returnedErr, fmt.Errorf("remove coding control directory: %w", err))
		}
	}()

	nonce, err := randomHex(24)
	if err != nil {
		return result, err
	}
	request := supervisorRequest{
		Schema: supervisorRequestSchema, Nonce: nonce, Mode: mode, CommandID: command.ID,
		CommandSHA256: commandSHA, Argv: append([]string(nil), command.Argv...),
		TimeoutMilliseconds: command.Timeout.Milliseconds(), ExpectedTotal: expectedTotal,
		CandidateUID: executor.config.CandidateUID, CandidateGID: executor.config.CandidateGID,
	}
	requestPath := filepath.Join(control, "request.json")
	responsePath := filepath.Join(control, "response.json")
	if err := writeRequest(requestPath, request); err != nil {
		return result, err
	}
	name := "dittobench-coding-" + executor.instanceID[len("coding-executor-"):len("coding-executor-")+12] + "-" + nonce[:12]
	cleanupTarget, cleanupUncertain := name, true
	defer func() {
		if cleanupErr := executor.cleanupContainer(cleanupTarget, cleanupUncertain); cleanupErr != nil {
			returnedErr = errors.Join(returnedErr, cleanupErr)
		}
	}()

	watchdog := command.Timeout + 30*time.Second
	runContext, cancel := context.WithTimeout(ctx, watchdog)
	containerID, err := executor.createContainer(runContext, name, mode, workspace, protected, control)
	if err != nil {
		cancel()
		return result, err
	}
	cleanupTarget, cleanupUncertain = containerID, false
	if err := executor.inspectContainerPolicy(runContext, containerID, mode, workspace, protected, control); err != nil {
		cancel()
		return result, err
	}
	started := time.Now()
	runErr := executor.docker.Run(runContext, "start", "--attach", containerID)
	result.duration = time.Since(started)
	cancel()
	terminal, terminalErr := executor.inspectTerminalState(containerID)
	if terminalErr != nil {
		return result, errors.Join(runErr, terminalErr)
	}
	if terminal.State.OOMKilled {
		result.response = supervisorResponse{
			Schema: supervisorResponseSchema, Nonce: request.Nonce, Mode: mode,
			CommandID: command.ID, CommandSHA256: commandSHA, ReturnCode: 137,
			Total: expectedTotal, Completed: true, ProcessTreeDead: true,
		}
		return result, nil
	}
	if runErr != nil {
		return result, fmt.Errorf("coding supervisor container failed: %w", runErr)
	}
	if terminal.State.Running || terminal.State.ExitCode != 0 {
		return result, errors.New("coding supervisor container terminal state is invalid")
	}
	response, err := readResponse(responsePath)
	if err != nil {
		return result, err
	}
	if err := response.validate(request, maxModelVisibleCommandOutput); err != nil {
		return result, err
	}
	result.response = response
	return result, nil
}

func (executor *Executor) createContainer(
	ctx context.Context,
	name string,
	mode executionMode,
	workspace string,
	protected string,
	control string,
) (string, error) {
	output, err := executor.docker.Output(ctx, executor.createArgs(name, mode, workspace, protected, control)...)
	if err != nil {
		return "", fmt.Errorf("create coding sandbox container: %w", err)
	}
	containerID := strings.TrimSpace(string(output))
	decoded, decodeErr := hex.DecodeString(containerID)
	if decodeErr != nil || len(decoded) != 32 || hex.EncodeToString(decoded) != containerID {
		return "", errors.New("coding Docker create returned an invalid container ID")
	}
	return containerID, nil
}

func removeControlDirectory(path string) error {
	var last error
	for range 3 {
		if err := os.RemoveAll(path); err == nil {
			return nil
		} else {
			last = err
		}
	}
	return last
}

func writeRequest(path string, request supervisorRequest) error {
	handle, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	encoder := json.NewEncoder(handle)
	encoder.SetEscapeHTML(false)
	encodeErr := encoder.Encode(request)
	syncErr := handle.Sync()
	closeErr := handle.Close()
	if encodeErr != nil || syncErr != nil || closeErr != nil {
		return errors.New("write coding supervisor request")
	}
	if err := os.Chmod(path, 0o400); err != nil {
		return errors.New("seal coding supervisor request")
	}
	return nil
}

func readResponse(path string) (supervisorResponse, error) {
	var response supervisorResponse
	fileDescriptor, err := unix.Open(path, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
	if err != nil {
		return response, errors.New("coding supervisor response file is unavailable")
	}
	handle := os.NewFile(uintptr(fileDescriptor), path)
	if handle == nil {
		_ = unix.Close(fileDescriptor)
		return response, errors.New("coding supervisor response handle is unavailable")
	}
	defer handle.Close()
	var stat unix.Stat_t
	if err := unix.Fstat(fileDescriptor, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG ||
		stat.Mode&0o022 != 0 || stat.Size <= 0 || stat.Size > maxSupervisorReceiptSize || stat.Uid != uint32(os.Getuid()) {
		return response, errors.New("coding supervisor response file is invalid")
	}
	decoder := json.NewDecoder(io.LimitReader(handle, maxSupervisorReceiptSize+1))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&response); err != nil {
		return response, errors.New("coding supervisor response JSON is invalid")
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return response, errors.New("coding supervisor response contains trailing content")
	}
	return response, nil
}

func (executor *Executor) createArgs(
	name string,
	mode executionMode,
	workspace string,
	protected string,
	control string,
) []string {
	policy := executor.config.Manifest.ResourcePolicy
	args := []string{
		"create", "--name", name, "--hostname", "coding-sandbox", "--pull", "never",
		"--label", "io.heyditto.dittobench.run=" + name,
		"--label", "io.heyditto.dittobench.coding-executor=" + executor.instanceID,
		"--platform", executor.config.Manifest.GraderPlatform,
		"--network", "none", "--read-only", "--ipc", "none", "--uts", "private", "--init",
		"--user", "0:0", "--cap-drop", "ALL",
		"--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "SETUID", "--cap-add", "SETGID",
		"--security-opt", "no-new-privileges", "--pids-limit", strconv.FormatUint(uint64(policy.PidsLimit), 10),
		"--memory", strconv.FormatUint(policy.MemoryLimitBytes, 10),
		"--memory-swap", strconv.FormatUint(policy.MemoryLimitBytes, 10),
		"--cpus", formatCPU(policy.CPUQuotaMillis), "--ulimit", "nofile=1024:1024",
		"--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=" + strconv.FormatUint(policy.ScratchLimitBytes, 10),
		"--log-driver", "none", "--stop-timeout", "1", "--workdir", workspaceMountPath,
	}
	if executor.config.SeccompProfile != "" {
		args = append(args, "--security-opt", "seccomp="+executor.config.SeccompProfile)
	}
	if executor.config.AppArmorProfile != "" {
		args = append(args, "--security-opt", "apparmor="+executor.config.AppArmorProfile)
	}
	workspaceMount := "type=bind,src=" + workspace + ",dst=" + workspaceMountPath + ",readonly"
	args = append(args, "--mount", workspaceMount)
	if mode == modeTest {
		args = append(args, "--mount", "type=bind,src="+protected+",dst="+protectedMountPath+",readonly")
	}
	args = append(args,
		"--mount", "type=bind,src="+control+",dst="+controlMountPath,
		"--entrypoint", executor.config.SupervisorPath,
		executor.config.ImageRef,
		"--request", controlMountPath+"/request.json",
		"--response", controlMountPath+"/response.json",
	)
	return args
}

func (executor *Executor) InstanceID() string {
	return executor.instanceID
}

func (executor *Executor) ImageReference() string {
	return executor.config.ImageRef
}

func (executor *Executor) RedactedConfig() map[string]any {
	return map[string]any{
		"image_digest":            executor.config.Manifest.GraderImageDigest,
		"platform":                executor.config.Manifest.GraderPlatform,
		"resource_profile_sha256": executor.config.Manifest.ResourceProfileSHA256,
		"network":                 "none",
		"candidate_uid":           executor.config.CandidateUID,
		"candidate_gid":           executor.config.CandidateGID,
		"supervisor":              filepath.Base(executor.config.SupervisorPath),
		"protected_mount":         protectedMountPath,
		"workspace_mount":         workspaceMountPath,
		"output_bound_bytes":      maxModelVisibleCommandOutput,
	}
}
