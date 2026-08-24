package codingexecutor

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

const (
	supervisorRequestSchema  = "dittobench-coding-supervisor-request-v1"
	supervisorResponseSchema = "dittobench-coding-supervisor-response-v1"
	defaultSupervisorPath    = "/usr/local/bin/dittobench-coding-supervisor"
	controlMountPath         = "/run/dittobench-control"
	workspaceMountPath       = "/workspace"
	protectedMountPath       = "/run/dittobench-grader"
	maxSupervisorReceiptSize = 64 << 10
	isolatedDaemonLabel      = "io.heyditto.dittobench.isolated=true"
	trustedTestDriverName    = "dittobench-test-driver"
)

type executionMode string

const (
	modeAuthoring executionMode = "authoring"
	modeBuild     executionMode = "build"
	modeTest      executionMode = "test"
)

// Config is immutable authority for one executor instance. Manifest and image
// identities are selected before the executor starts; the miner controls none
// of these fields.
type Config struct {
	Manifest                codinggrader.Manifest
	ImageRef                string
	AuthoringOnly           bool
	SupervisorPath          string
	CandidateUID            uint32
	CandidateGID            uint32
	RequireRootless         bool
	RequireIsolatedDaemon   bool
	AllowCertificationImage bool
	SeccompProfile          string
	AppArmorProfile         string
}

func (config Config) validate() error {
	if config.AuthoringOnly {
		if config.Manifest.ResourcePolicy.Validate() != nil ||
			!ociDigest(config.Manifest.GraderImageDigest) ||
			config.Manifest.GraderPlatform != "linux/amd64" {
			return errors.New("coding authoring executor authority is invalid")
		}
	} else if err := config.Manifest.Validate(time.Now()); err != nil {
		return fmt.Errorf("coding executor manifest: %w", err)
	}
	if (!config.AuthoringOnly && config.Manifest.GraderContractSHA256 != codinggrader.GraderContractSHA256()) ||
		config.Manifest.GraderPlatform != "linux/amd64" ||
		!strings.HasSuffix(config.ImageRef, "@"+config.Manifest.GraderImageDigest) ||
		strings.Count(config.ImageRef, "@") != 1 || strings.HasPrefix(config.ImageRef, "-") ||
		strings.ContainsAny(config.ImageRef, " ,\t\r\n\x00") ||
		!config.RequireRootless || !config.RequireIsolatedDaemon {
		return errors.New("coding executor identity or daemon policy is invalid")
	}
	if !config.AuthoringOnly {
		planSHA, err := codinggrader.GraderPlanSHA256(config.Manifest)
		if err != nil || planSHA != config.Manifest.GraderPlanSHA256 {
			return errors.New("coding executor grader plan is invalid")
		}
		resourceSHA, err := codinggrader.ResourceProfileSHA256(config.Manifest.ResourcePolicy)
		if err != nil || resourceSHA != config.Manifest.ResourceProfileSHA256 {
			return errors.New("coding executor resource profile is invalid")
		}
	}
	if config.Manifest.ResourcePolicy.ScratchLimitBytes <
		uint64(config.Manifest.ResourcePolicy.CandidateLimits.MaxWorkspaceBytes) {
		return errors.New("coding executor scratch cannot hold the bounded candidate workspace")
	}
	if !config.AuthoringOnly {
		for _, group := range config.Manifest.TestGroups {
			if len(group.Command.Argv) == 0 || group.Command.Argv[0] != trustedTestDriverName {
				return errors.New("coding executor test command does not use the trusted driver")
			}
		}
	}
	if config.SupervisorPath == "" {
		config.SupervisorPath = defaultSupervisorPath
	}
	if config.SupervisorPath != defaultSupervisorPath || config.CandidateUID == 0 || config.CandidateGID == 0 ||
		config.CandidateUID > 1<<31 || config.CandidateGID > 1<<31 ||
		!validProfileName(config.SeccompProfile) || !validProfileName(config.AppArmorProfile) {
		return errors.New("coding executor supervisor or process identity is invalid")
	}
	return nil
}

func normalizeConfig(config Config) Config {
	if config.SupervisorPath == "" {
		config.SupervisorPath = defaultSupervisorPath
	}
	config.Manifest = cloneManifest(config.Manifest)
	return config
}

func cloneManifest(manifest codinggrader.Manifest) codinggrader.Manifest {
	manifest.Build.Command.Argv = append([]string(nil), manifest.Build.Command.Argv...)
	manifest.TestGroups = append([]codinggrader.TestGroupSpec(nil), manifest.TestGroups...)
	for index := range manifest.TestGroups {
		manifest.TestGroups[index].Command.Argv = append([]string(nil), manifest.TestGroups[index].Command.Argv...)
	}
	return manifest
}

func validProfileName(value string) bool {
	if value == "" {
		return true
	}
	if len(value) > 256 || !utf8.ValidString(value) || strings.ContainsAny(value, `/\\,\x00`) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func ociDigest(value string) bool {
	if !strings.HasPrefix(value, "sha256:") || len(value) != len("sha256:")+64 {
		return false
	}
	digest := strings.TrimPrefix(value, "sha256:")
	if digest != strings.ToLower(digest) {
		return false
	}
	_, err := hex.DecodeString(digest)
	return err == nil
}

type supervisorRequest struct {
	Schema              string        `json:"schema"`
	Nonce               string        `json:"nonce"`
	Mode                executionMode `json:"mode"`
	CommandID           string        `json:"command_id"`
	CommandSHA256       string        `json:"command_sha256"`
	Argv                []string      `json:"argv"`
	TimeoutMilliseconds int64         `json:"timeout_milliseconds"`
	ExpectedTotal       uint32        `json:"expected_total"`
	CandidateUID        uint32        `json:"candidate_uid"`
	CandidateGID        uint32        `json:"candidate_gid"`
}

type supervisorResponse struct {
	Schema           string        `json:"schema"`
	Nonce            string        `json:"nonce"`
	Mode             executionMode `json:"mode"`
	CommandID        string        `json:"command_id"`
	CommandSHA256    string        `json:"command_sha256"`
	ReturnCode       int           `json:"returncode"`
	Passed           uint32        `json:"passed"`
	Total            uint32        `json:"total"`
	Completed        bool          `json:"completed"`
	TimedOut         bool          `json:"timed_out"`
	Stdout           string        `json:"stdout"`
	Stderr           string        `json:"stderr"`
	WorkspaceMutated bool          `json:"workspace_mutated"`
	ProcessTreeDead  bool          `json:"process_tree_dead"`
}

func (response supervisorResponse) validate(request supervisorRequest, maximumOutput int) error {
	if response.Schema != supervisorResponseSchema || response.Nonce != request.Nonce || response.Mode != request.Mode ||
		response.CommandID != request.CommandID || response.CommandSHA256 != request.CommandSHA256 ||
		!response.ProcessTreeDead || response.Passed > response.Total || response.Total != request.ExpectedTotal ||
		len(response.Stdout) > maximumOutput || len(response.Stderr) > maximumOutput ||
		!utf8.ValidString(response.Stdout) || !utf8.ValidString(response.Stderr) {
		return errors.New("coding supervisor receipt is invalid")
	}
	if request.Mode != modeAuthoring && (response.Stdout != "" || response.Stderr != "") {
		return errors.New("coding grader receipt exposed candidate output")
	}
	if request.Mode != modeAuthoring && response.WorkspaceMutated {
		return errors.New("coding grader receipt reported an authoring mutation")
	}
	if response.TimedOut == response.Completed {
		return errors.New("coding supervisor timeout receipt is incoherent")
	}
	return nil
}

func resolveWorkspace(raw string) (string, error) {
	if !validMountPath(raw) || !filepath.IsAbs(raw) {
		return "", errors.New("coding executor workspace path is invalid")
	}
	resolved, err := filepath.EvalSymlinks(raw)
	if err != nil || !filepath.IsAbs(resolved) || resolved == string(filepath.Separator) || !validMountPath(resolved) {
		return "", errors.New("coding executor workspace path is unavailable")
	}
	info, err := os.Stat(resolved)
	if err != nil || !info.IsDir() {
		return "", errors.New("coding executor workspace is not a directory")
	}
	return resolved, nil
}

func validMountPath(value string) bool {
	if value == "" || !utf8.ValidString(value) || strings.ContainsAny(value, ",\x00") {
		return false
	}
	for _, character := range value {
		if unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func pathsOverlap(left, right string) bool {
	relative, err := filepath.Rel(left, right)
	if err == nil && (relative == "." || (relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)))) {
		return true
	}
	relative, err = filepath.Rel(right, left)
	return err == nil && (relative == "." || (relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))))
}

type dockerCLI interface {
	Output(ctx context.Context, args ...string) ([]byte, error)
	Run(ctx context.Context, args ...string) error
}

type executionResult struct {
	response supervisorResponse
	duration time.Duration
}

func commandDigest(command codingrunner.CommandSpec) (string, error) {
	if err := command.Validate(); err != nil {
		return "", err
	}
	return codinggrader.CommandSHA256(command.ID, command.Argv, command.Timeout.Milliseconds())
}

func safeDiagnostic(text string, paths ...string) string {
	for _, path := range paths {
		if path != "" {
			text = strings.ReplaceAll(text, path, "<workspace>")
		}
	}
	return text
}

func formatCPU(millis uint32) string {
	return fmt.Sprintf("%d.%03d", millis/1000, millis%1000)
}
