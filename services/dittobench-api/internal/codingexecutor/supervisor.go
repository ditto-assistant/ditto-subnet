package codingexecutor

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"golang.org/x/sys/unix"
)

const supervisorKillGrace = 5 * time.Second

const trustedTestReportSchema = "dittobench-coding-trusted-test-report-v1"

type trustedTestReport struct {
	Schema    string `json:"schema"`
	Nonce     string `json:"nonce"`
	Passed    uint32 `json:"passed"`
	Total     uint32 `json:"total"`
	Completed bool   `json:"completed"`
}

// SupervisorMain runs the trusted container-parent contract. It returns an
// error only when authoritative process/receipt evidence cannot be produced;
// candidate command failures are encoded in the response and return nil.
func SupervisorMain(ctx context.Context, arguments []string) error {
	return supervisorMainAt(ctx, arguments, workspaceMountPath)
}

func supervisorMainAt(ctx context.Context, arguments []string, workspace string) error {
	if ctx == nil {
		return errors.New("coding supervisor context is required")
	}
	flags := flag.NewFlagSet("dittobench-coding-supervisor", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	requestPath := flags.String("request", "", "sealed supervisor request")
	responsePath := flags.String("response", "", "exclusive supervisor response")
	if err := flags.Parse(arguments); err != nil || len(flags.Args()) != 0 {
		return errors.New("coding supervisor arguments are invalid")
	}
	if err := validateControlPaths(*requestPath, *responsePath); err != nil {
		return err
	}
	request, err := readSupervisorRequest(*requestPath)
	if err != nil {
		return err
	}
	testReportPath := filepath.Join(filepath.Dir(*requestPath), "test-report.json")
	response, err := executeSupervisorRequestAt(ctx, request, testReportPath, workspace)
	if err != nil {
		return err
	}
	return writeSupervisorResponse(*responsePath, response)
}

func validateControlPaths(requestPath, responsePath string) error {
	if !filepath.IsAbs(requestPath) || !filepath.IsAbs(responsePath) || requestPath == responsePath ||
		filepath.Dir(requestPath) != filepath.Dir(responsePath) || filepath.Base(requestPath) != "request.json" ||
		filepath.Base(responsePath) != "response.json" || !validMountPath(filepath.Dir(requestPath)) {
		return errors.New("coding supervisor control paths are invalid")
	}
	return nil
}

func readSupervisorRequest(path string) (supervisorRequest, error) {
	var request supervisorRequest
	fileDescriptor, err := unix.Open(path, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
	if err != nil {
		return request, errors.New("coding supervisor request is unavailable")
	}
	handle := os.NewFile(uintptr(fileDescriptor), path)
	if handle == nil {
		_ = unix.Close(fileDescriptor)
		return request, errors.New("coding supervisor request handle is unavailable")
	}
	defer handle.Close()
	var stat unix.Stat_t
	if err := unix.Fstat(fileDescriptor, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG ||
		stat.Mode&0o177 != 0 || stat.Size <= 0 || stat.Size > maxSupervisorReceiptSize {
		return request, errors.New("coding supervisor request file is invalid")
	}
	decoder := json.NewDecoder(io.LimitReader(handle, maxSupervisorReceiptSize+1))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		return request, errors.New("coding supervisor request JSON is invalid")
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return request, errors.New("coding supervisor request contains trailing content")
	}
	if err := request.validate(); err != nil {
		return request, err
	}
	return request, nil
}

func (request supervisorRequest) validate() error {
	decodedNonce, nonceErr := hex.DecodeString(request.Nonce)
	command := codingrunner.CommandSpec{
		ID: request.CommandID, Argv: append([]string(nil), request.Argv...),
		Timeout: time.Duration(request.TimeoutMilliseconds) * time.Millisecond,
	}
	commandSHA, commandErr := commandDigest(command)
	if request.Schema != supervisorRequestSchema || nonceErr != nil || len(decodedNonce) != 24 ||
		hex.EncodeToString(decodedNonce) != request.Nonce || commandErr != nil || commandSHA != request.CommandSHA256 ||
		request.CandidateUID == 0 || request.CandidateGID == 0 {
		return errors.New("coding supervisor request authority is invalid")
	}
	switch request.Mode {
	case modeAuthoring, modeBuild:
		if request.ExpectedTotal != 0 {
			return errors.New("coding supervisor non-test request contains a test count")
		}
	case modeTest:
		if request.ExpectedTotal == 0 || request.Argv[0] != trustedTestDriverName {
			return errors.New("coding supervisor test request lacks trusted driver authority")
		}
	default:
		return errors.New("coding supervisor execution mode is invalid")
	}
	return nil
}

func executeSupervisorRequestAt(
	ctx context.Context,
	request supervisorRequest,
	testReportPath string,
	workspace string,
) (response supervisorResponse, returnedErr error) {
	response = supervisorResponse{
		Schema: supervisorResponseSchema, Nonce: request.Nonce, Mode: request.Mode,
		CommandID: request.CommandID, CommandSHA256: request.CommandSHA256,
		Total: request.ExpectedTotal,
	}
	childWorkspace, cleanupWorkspace, err := prepareChildWorkspace(
		request.Mode, workspace, request.CandidateUID, request.CandidateGID,
	)
	if err != nil {
		return response, err
	}
	beforeWorkspaceSHA := ""
	if request.Mode == modeAuthoring {
		beforeWorkspaceSHA, err = regularTreeSHA256(childWorkspace)
		if err != nil {
			return response, err
		}
	}
	defer func() {
		if cleanupErr := cleanupWorkspace(); cleanupErr != nil {
			returnedErr = errors.Join(returnedErr, cleanupErr)
		}
	}()
	commandContext, cancel := context.WithTimeout(ctx, time.Duration(request.TimeoutMilliseconds)*time.Millisecond)
	defer cancel()
	childArguments := append([]string(nil), request.Argv[1:]...)
	if request.Mode == modeTest {
		if _, err := os.Lstat(testReportPath); !errors.Is(err, os.ErrNotExist) {
			return response, errors.New("coding trusted test report path is not exclusive")
		}
		childArguments = append(childArguments,
			"--dittobench-report", testReportPath,
			"--dittobench-nonce", request.Nonce,
			"--dittobench-expected", fmt.Sprint(request.ExpectedTotal),
			"--dittobench-candidate-uid", fmt.Sprint(request.CandidateUID),
			"--dittobench-candidate-gid", fmt.Sprint(request.CandidateGID),
		)
	}
	command := exec.CommandContext(commandContext, request.Argv[0], childArguments...)
	command.Dir = childWorkspace
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if request.Mode != modeTest &&
		(request.CandidateUID != uint32(os.Getuid()) || request.CandidateGID != uint32(os.Getgid())) {
		if os.Geteuid() != 0 {
			return response, errors.New("coding supervisor cannot assume the candidate identity")
		}
		command.SysProcAttr.Credential = &syscall.Credential{
			Uid: request.CandidateUID, Gid: request.CandidateGID, NoSetGroups: true,
		}
	}
	var stdout, stderr boundedBuffer
	stdout.maximum, stderr.maximum = maxModelVisibleCommandOutput, maxModelVisibleCommandOutput
	if request.Mode == modeAuthoring {
		command.Stdout, command.Stderr = &stdout, &stderr
	} else {
		command.Stdout, command.Stderr = io.Discard, io.Discard
	}
	command.WaitDelay = supervisorKillGrace
	command.Cancel = func() error {
		if command.Process == nil {
			return nil
		}
		if err := unix.Kill(-command.Process.Pid, unix.SIGKILL); err != nil && !errors.Is(err, unix.ESRCH) {
			return err
		}
		return nil
	}
	startErr := command.Start()
	if startErr != nil {
		return response, fmt.Errorf("start coding candidate command: %w", startErr)
	}
	processGroup := command.Process.Pid
	waitErr := command.Wait()
	response.TimedOut = errors.Is(commandContext.Err(), context.DeadlineExceeded)
	response.Completed = !response.TimedOut
	response.ReturnCode = childReturnCode(waitErr, response.TimedOut)
	if request.Mode == modeAuthoring {
		response.Stdout = strings.ReplaceAll(boundedUTF8(stdout.Bytes(), stdout.overflow), childWorkspace, "<workspace>")
		response.Stderr = strings.ReplaceAll(boundedUTF8(stderr.Bytes(), stderr.overflow), childWorkspace, "<workspace>")
	}
	response.ProcessTreeDead = killAndVerifyProcessGroup(processGroup)
	if !response.ProcessTreeDead {
		return response, errors.New("coding supervisor could not prove process-tree termination")
	}
	if request.Mode == modeAuthoring {
		afterWorkspaceSHA, snapshotErr := regularTreeSHA256(childWorkspace)
		response.WorkspaceMutated = snapshotErr != nil || afterWorkspaceSHA != beforeWorkspaceSHA
	}
	if request.Mode == modeTest && response.Completed {
		report, err := readTrustedTestReport(testReportPath, request)
		if err != nil {
			return response, err
		}
		response.Passed = report.Passed
	}
	return response, nil
}

func prepareChildWorkspace(
	mode executionMode,
	workspace string,
	candidateUID uint32,
	candidateGID uint32,
) (string, func() error, error) {
	workspace, err := filepath.EvalSymlinks(workspace)
	if err != nil || !filepath.IsAbs(workspace) {
		return "", nil, errors.New("coding supervisor workspace is unavailable")
	}
	info, err := os.Stat(workspace)
	if err != nil || !info.IsDir() {
		return "", nil, errors.New("coding supervisor workspace is not a directory")
	}
	switch mode {
	case modeAuthoring, modeBuild:
		copyRoot, err := os.MkdirTemp("", "dittobench-coding-build-workspace-")
		if err != nil {
			return "", nil, err
		}
		if err := copyRegularTree(workspace, copyRoot); err != nil {
			_ = os.RemoveAll(copyRoot)
			return "", nil, err
		}
		if err := chownTree(copyRoot, int(candidateUID), int(candidateGID)); err != nil {
			_ = os.RemoveAll(copyRoot)
			return "", nil, err
		}
		return copyRoot, func() error { return os.RemoveAll(copyRoot) }, nil
	case modeTest:
		return workspace, func() error { return nil }, nil
	default:
		return "", nil, errors.New("coding supervisor workspace mode is invalid")
	}
}

func regularTreeSHA256(root string) (string, error) {
	hasher := sha256.New()
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if !info.IsDir() && !info.Mode().IsRegular() {
			return errors.New("coding workspace contains a special entry")
		}
		_, _ = fmt.Fprintf(hasher, "%s\x00%d\x00%d\x00", filepath.ToSlash(relative), info.Mode().Perm(), info.Size())
		if info.Mode().IsRegular() {
			handle, err := os.Open(path)
			if err != nil {
				return err
			}
			_, copyErr := io.Copy(hasher, handle)
			closeErr := handle.Close()
			if copyErr != nil || closeErr != nil {
				return errors.New("hash coding workspace file")
			}
		}
		_, _ = hasher.Write([]byte{0})
		return nil
	})
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(hasher.Sum(nil)), nil
}

func chownTree(root string, uid, gid int) error {
	var paths []string
	if err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if !info.IsDir() && !info.Mode().IsRegular() {
			return errors.New("coding supervisor workspace contains a special entry")
		}
		paths = append(paths, path)
		return nil
	}); err != nil {
		return err
	}
	for index := len(paths) - 1; index >= 0; index-- {
		if err := os.Lchown(paths[index], uid, gid); err != nil {
			return fmt.Errorf("set coding workspace ownership: %w", err)
		}
	}
	return nil
}

func copyRegularTree(source, destination string) error {
	return filepath.Walk(source, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		target := filepath.Join(destination, relative)
		if info.IsDir() {
			if relative == "." {
				return os.Chmod(destination, info.Mode().Perm())
			}
			if err := os.Mkdir(target, info.Mode().Perm()); err != nil {
				return err
			}
			return os.Chmod(target, info.Mode().Perm())
		}
		if !info.Mode().IsRegular() {
			return errors.New("coding build workspace contains a special entry")
		}
		input, err := os.Open(path)
		if err != nil {
			return err
		}
		output, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, info.Mode().Perm())
		if err != nil {
			input.Close()
			return err
		}
		_, copyErr := io.Copy(output, input)
		inputCloseErr := input.Close()
		outputSyncErr := output.Sync()
		outputCloseErr := output.Close()
		if copyErr != nil || inputCloseErr != nil || outputSyncErr != nil || outputCloseErr != nil {
			return errors.New("copy coding build workspace")
		}
		return os.Chmod(target, info.Mode().Perm())
	})
}

func readTrustedTestReport(path string, request supervisorRequest) (trustedTestReport, error) {
	var report trustedTestReport
	fileDescriptor, err := unix.Open(path, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
	if err != nil {
		return report, errors.New("trusted test driver did not produce a report")
	}
	handle := os.NewFile(uintptr(fileDescriptor), path)
	if handle == nil {
		_ = unix.Close(fileDescriptor)
		return report, errors.New("trusted test report handle is unavailable")
	}
	defer handle.Close()
	var stat unix.Stat_t
	if err := unix.Fstat(fileDescriptor, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG ||
		stat.Mode&0o077 != 0 || stat.Size <= 0 || stat.Size > 4096 || stat.Uid != uint32(os.Getuid()) {
		return report, errors.New("trusted test report file is invalid")
	}
	decoder := json.NewDecoder(io.LimitReader(handle, 4097))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&report); err != nil {
		return report, errors.New("trusted test report JSON is invalid")
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return report, errors.New("trusted test report contains trailing content")
	}
	if report.Schema != trustedTestReportSchema || report.Nonce != request.Nonce || !report.Completed ||
		report.Total != request.ExpectedTotal || report.Passed > report.Total {
		return report, errors.New("trusted test report authority is invalid")
	}
	return report, nil
}

func childReturnCode(waitErr error, timedOut bool) int {
	if timedOut {
		return 124
	}
	if waitErr == nil {
		return 0
	}
	var exitErr *exec.ExitError
	if errors.As(waitErr, &exitErr) {
		return exitErr.ExitCode()
	}
	return 125
}

func boundedUTF8(value []byte, overflow bool) string {
	text := strings.ToValidUTF8(string(value), "\uFFFD")
	if len(text) > maxModelVisibleCommandOutput {
		overflow = true
	}
	if overflow {
		const marker = "\n<output-truncated>"
		maximum := maxModelVisibleCommandOutput - len(marker)
		if maximum < 0 {
			maximum = 0
		}
		for maximum > 0 && !utf8.ValidString(text[:min(maximum, len(text))]) {
			maximum--
		}
		text = text[:min(maximum, len(text))] + marker
	}
	return text
}

func killAndVerifyProcessGroup(processGroup int) bool {
	if processGroup <= 0 {
		return false
	}
	if err := unix.Kill(-processGroup, unix.SIGKILL); err != nil && !errors.Is(err, unix.ESRCH) {
		return false
	}
	for range 20 {
		err := unix.Kill(-processGroup, 0)
		if errors.Is(err, unix.ESRCH) {
			return true
		}
		if err != nil && !errors.Is(err, unix.EPERM) {
			return false
		}
		time.Sleep(10 * time.Millisecond)
	}
	return false
}

func writeSupervisorResponse(path string, response supervisorResponse) error {
	handle, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return errors.New("create coding supervisor response")
	}
	var body bytes.Buffer
	encoder := json.NewEncoder(&body)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(response); err != nil || body.Len() > maxSupervisorReceiptSize {
		handle.Close()
		return errors.New("encode coding supervisor response")
	}
	written, writeErr := handle.Write(body.Bytes())
	syncErr := handle.Sync()
	closeErr := handle.Close()
	if writeErr != nil || written != body.Len() || syncErr != nil || closeErr != nil {
		return errors.New("write coding supervisor response")
	}
	return nil
}
