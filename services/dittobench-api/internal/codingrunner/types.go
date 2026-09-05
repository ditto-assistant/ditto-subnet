package codingrunner

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

const (
	ContractVersion = 1
	// HostedContractVersion is opt-in through the separate hosted entry points.
	HostedContractVersion = 2

	DefaultMaxBundleBytes      int64 = 512 << 20
	DefaultMaxWorkspaceBytes   int64 = 1 << 30
	DefaultMaxFileBytes        int64 = 32 << 20
	DefaultMaxPatchBytes       int64 = 32 << 20
	DefaultMaxEntries                = 50_000
	DefaultMaxToolCalls              = 256
	DefaultMaxReadBytes              = 32 << 10
	DefaultMaxResponseBytes          = 256 << 10
	DefaultMaxSearchResults          = 100
	DefaultMaxReplayCacheBytes       = 64 << 20
	DefaultMaxTranscriptBytes        = 128 << 20
	MaxEditTextBytes                 = 64 << 10
	MaxToolRequestBytes        int64 = 64 << 10

	hardMaxBundleBytes        int64 = 2 << 30
	hardMaxWorkspaceBytes     int64 = 4 << 30
	hardMaxFileBytes          int64 = 128 << 20
	hardMaxPatchBytes         int64 = 128 << 20
	hardMaxEntries                  = 200_000
	hardMaxToolCalls                = 1_000
	hardMaxReadBytes                = 256 << 10
	hardMaxResponseBytes            = 2 << 20
	hardMaxSearchResults            = 1_000
	hardMaxReplayCacheBytes   int64 = 512 << 20
	hardMaxTranscriptBytes    int64 = 512 << 20
	maxCommandTimeout               = 10 * time.Minute
	maxSessionLifetime              = 2 * time.Hour
	toolResponseEnvelopeBytes       = 2 << 10
)

var (
	errSessionClosed      = errors.New("coding workspace is closed")
	errCapabilityRevoked  = errors.New("coding workspace capability is revoked")
	errCapabilityExpired  = errors.New("coding workspace capability expired")
	errCapabilityIdentity = errors.New("coding workspace capability identity mismatch")
	errToolBudget         = errors.New("coding workspace tool-call budget exhausted")
	errCallIDConflict     = errors.New("coding workspace call_id was reused with different bytes")
)

// Limits are the runner-enforced portion of the signed resource profile.
// Every value is required to remain below a compile-time hard ceiling.
type Limits struct {
	MaxBundleBytes      int64
	MaxWorkspaceBytes   int64
	MaxFileBytes        int64
	MaxPatchBytes       int64
	MaxEntries          int
	MaxToolCalls        uint32
	MaxReadBytes        int
	MaxResponseBytes    int
	MaxSearchResults    int
	MaxReplayCacheBytes int64
	MaxTranscriptBytes  int64
}

// DefaultLimits returns a conservative one-task authoring envelope.
func DefaultLimits() Limits {
	return Limits{
		MaxBundleBytes:      DefaultMaxBundleBytes,
		MaxWorkspaceBytes:   DefaultMaxWorkspaceBytes,
		MaxFileBytes:        DefaultMaxFileBytes,
		MaxPatchBytes:       DefaultMaxPatchBytes,
		MaxEntries:          DefaultMaxEntries,
		MaxToolCalls:        DefaultMaxToolCalls,
		MaxReadBytes:        DefaultMaxReadBytes,
		MaxResponseBytes:    DefaultMaxResponseBytes,
		MaxSearchResults:    DefaultMaxSearchResults,
		MaxReplayCacheBytes: DefaultMaxReplayCacheBytes,
		MaxTranscriptBytes:  DefaultMaxTranscriptBytes,
	}
}

// Validate checks the complete hard and aggregate resource envelope.
func (limits Limits) Validate() error {
	if limits.MaxBundleBytes <= 0 || limits.MaxBundleBytes > hardMaxBundleBytes ||
		limits.MaxWorkspaceBytes <= 0 || limits.MaxWorkspaceBytes > hardMaxWorkspaceBytes ||
		limits.MaxFileBytes <= 0 || limits.MaxFileBytes > hardMaxFileBytes ||
		limits.MaxPatchBytes <= 0 || limits.MaxPatchBytes > hardMaxPatchBytes ||
		limits.MaxEntries <= 0 || limits.MaxEntries > hardMaxEntries ||
		limits.MaxToolCalls == 0 || limits.MaxToolCalls > hardMaxToolCalls ||
		limits.MaxReadBytes <= 0 || limits.MaxReadBytes > hardMaxReadBytes ||
		limits.MaxResponseBytes < 4096 || limits.MaxResponseBytes > hardMaxResponseBytes ||
		limits.MaxSearchResults <= 0 || limits.MaxSearchResults > hardMaxSearchResults ||
		limits.MaxReplayCacheBytes <= 0 || limits.MaxReplayCacheBytes > hardMaxReplayCacheBytes ||
		limits.MaxTranscriptBytes <= 0 || limits.MaxTranscriptBytes > hardMaxTranscriptBytes {
		return errors.New("coding runner limits exceed the hard resource envelope")
	}
	if limits.MaxFileBytes > limits.MaxWorkspaceBytes || limits.MaxPatchBytes > limits.MaxWorkspaceBytes {
		return errors.New("coding runner file or patch limit exceeds workspace limit")
	}
	if limits.MaxReadBytes > limits.MaxResponseBytes-toolResponseEnvelopeBytes {
		return errors.New("coding runner read limit exceeds response limit")
	}
	if int64(limits.MaxToolCalls) > limits.MaxReplayCacheBytes/int64(limits.MaxResponseBytes) {
		return errors.New("coding runner replay-cache budget cannot retain every allowed response")
	}
	// Raw U+2028/U+2029 input can double when canonical JSON escapes it.
	maxEventBytes := 2*MaxToolRequestBytes + int64(limits.MaxResponseBytes) + 8192
	if int64(limits.MaxToolCalls) > limits.MaxTranscriptBytes/maxEventBytes {
		return errors.New("coding runner transcript budget cannot retain every allowed event")
	}
	return nil
}

// CommandSpec is a manifest-addressed command. Arguments are never supplied by
// the miner; the tool request carries only ID.
type CommandSpec struct {
	ID      string
	Argv    []string
	Timeout time.Duration
}

// Validate checks the bounded manifest-owned command contract.
func (command CommandSpec) Validate() error {
	if !validIdentifier(command.ID, 80) || len(command.Argv) == 0 || len(command.Argv) > 64 ||
		command.Timeout <= 0 || command.Timeout > maxCommandTimeout || command.Timeout%time.Millisecond != 0 {
		return errors.New("coding command is outside contract bounds")
	}
	executable := command.Argv[0]
	if !validIdentifier(executable, 128) || strings.ContainsAny(executable, `/\\`) {
		return errors.New("coding command executable must be a bare identifier")
	}
	switch strings.ToLower(executable) {
	case "bash", "cmd", "dash", "env", "fish", "powershell", "pwsh", "sh", "zsh":
		return errors.New("coding command may not invoke a general shell")
	}
	for _, argument := range command.Argv[1:] {
		if argument == "" || len(argument) > 4096 || !utf8.ValidString(argument) || strings.ContainsRune(argument, 0) {
			return errors.New("coding command argument is outside contract bounds")
		}
	}
	var argumentBytes int
	for _, argument := range command.Argv {
		argumentBytes += len(argument)
	}
	if argumentBytes > 8192 {
		return errors.New("coding command argv exceeds 8192 UTF-8 bytes")
	}
	return nil
}

// Manifest is the task-scoped authority consumed by one runner session.
// Platform transport URLs and grader material are intentionally absent.
type Manifest struct {
	CodingContractVersion int
	TicketID              string
	CaseID                string
	ProfileCapabilityID   string
	VisibleBundleSHA256   string
	BaseTreeSHA256        string
	Deadline              time.Time
	EditablePaths         []string
	CreatablePaths        []string
	DeletablePaths        []string
	TestCommands          []CommandSpec
	BuildCommands         []CommandSpec
	Limits                Limits
}

func (manifest Manifest) validate(now time.Time) error {
	return manifest.validateVersion(now, ContractVersion)
}

func (manifest Manifest) validateVersion(now time.Time, version int) error {
	if (version != ContractVersion && version != HostedContractVersion) || manifest.CodingContractVersion != version ||
		!validIdentifier(manifest.TicketID, 256) || !validIdentifier(manifest.CaseID, 256) ||
		!validIdentifier(manifest.ProfileCapabilityID, 256) ||
		!isLowerSHA256(manifest.VisibleBundleSHA256) || !isLowerSHA256(manifest.BaseTreeSHA256) {
		return errors.New("coding runner manifest identity is invalid")
	}
	if manifest.Deadline.IsZero() || !manifest.Deadline.After(now) || manifest.Deadline.After(now.Add(maxSessionLifetime)) {
		return errors.New("coding runner deadline is outside the bounded session lifetime")
	}
	if err := manifest.Limits.Validate(); err != nil {
		return err
	}
	if len(manifest.EditablePaths)+len(manifest.CreatablePaths)+len(manifest.DeletablePaths) > manifest.Limits.MaxEntries {
		return errors.New("coding runner path policy exceeds the signed entry envelope")
	}
	if len(manifest.TestCommands)+len(manifest.BuildCommands) > int(manifest.Limits.MaxToolCalls) {
		return errors.New("coding runner command policy exceeds the signed call envelope")
	}
	pathSets := [][]string{manifest.EditablePaths, manifest.CreatablePaths, manifest.DeletablePaths}
	seenPaths := make(map[string]struct{})
	for _, values := range pathSets {
		if values == nil || !slices.IsSorted(values) {
			return errors.New("coding runner path policies must be present and sorted")
		}
		for _, value := range values {
			if _, err := safePath(value, false); err != nil {
				return err
			}
			if _, duplicate := seenPaths[value]; duplicate {
				return errors.New("coding runner path policies overlap or contain duplicates")
			}
			seenPaths[value] = struct{}{}
		}
	}
	if err := validateCommands(manifest.TestCommands); err != nil {
		return fmt.Errorf("test commands: %w", err)
	}
	if err := validateCommands(manifest.BuildCommands); err != nil {
		return fmt.Errorf("build commands: %w", err)
	}
	testIDs := commandIDs(manifest.TestCommands)
	for id := range commandIDs(manifest.BuildCommands) {
		if _, duplicate := testIDs[id]; duplicate {
			return errors.New("test and build command IDs must be disjoint")
		}
	}
	return nil
}

// Validate checks the complete runner manifest at the supplied trusted time.
// It lets orchestration reject invalid control-plane input before contacting an
// untrusted harness or consuming a visible bundle.
func (manifest Manifest) Validate(now time.Time) error {
	return manifest.validate(now)
}

func validateCommands(commands []CommandSpec) error {
	if commands == nil {
		return errors.New("command collection must be present")
	}
	previous := ""
	for _, command := range commands {
		if err := command.Validate(); err != nil {
			return err
		}
		if previous != "" && command.ID <= previous {
			return errors.New("commands must be unique and sorted by ID")
		}
		previous = command.ID
	}
	return nil
}

func commandIDs(commands []CommandSpec) map[string]struct{} {
	result := make(map[string]struct{}, len(commands))
	for _, command := range commands {
		result[command.ID] = struct{}{}
	}
	return result
}

// CommandResult is bounded and scrubbed before it becomes model-visible.
type CommandResult struct {
	ReturnCode       int           `json:"returncode"`
	Stdout           string        `json:"stdout"`
	Stderr           string        `json:"stderr"`
	TimedOut         bool          `json:"timed_out"`
	WorkspaceMutated bool          `json:"workspace_mutated"`
	Duration         time.Duration `json:"-"`
}

// CommandExecutor runs one manifest-owned command in the trusted runner
// sandbox. Implementations must deny network, kill the complete process group
// on cancellation, prevent background survivors, and confine all filesystem
// access to the supplied workspace plus bounded scratch space. The supplied
// host path is trusted mount input; candidate processes must see a fixed path
// such as /workspace rather than this validator-local name.
type CommandExecutor interface {
	Execute(ctx context.Context, workspace string, command CommandSpec) (CommandResult, error)
}

// ToolRequest is the miner-facing typed workspace request.
type ToolRequest struct {
	CodingContractVersion int             `json:"coding_contract_version"`
	CaseID                string          `json:"case_id"`
	ProfileCapabilityID   string          `json:"profile_capability_id"`
	CallID                string          `json:"call_id"`
	Name                  string          `json:"name"`
	Arguments             json.RawMessage `json:"arguments"`
}

// ToolError is a bounded model-visible tool failure.
type ToolError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// ToolResponse is the authoritative sequence/event receipt for one call.
type ToolResponse struct {
	CallID      string          `json:"call_id"`
	Sequence    uint64          `json:"sequence"`
	OK          bool            `json:"ok"`
	Result      json.RawMessage `json:"result"`
	Error       *ToolError      `json:"error"`
	EventSHA256 string          `json:"event_sha256"`
}

// FrozenChange is one replayable full-file transition. A nil side represents
// file creation or deletion; exact before/after hashes prevent fuzzy apply.
type FrozenChange struct {
	Path         string  `json:"path"`
	Kind         string  `json:"kind"`
	Mode         uint32  `json:"mode"`
	BeforeSHA256 *string `json:"before_sha256"`
	AfterSHA256  *string `json:"after_sha256"`
	AfterContent []byte  `json:"after_content"`
}

// FrozenSubmission is the successful authoritative output of authoring.
type FrozenSubmission struct {
	CodingContractVersion     int            `json:"coding_contract_version"`
	CaseID                    string         `json:"case_id"`
	BaseTreeSHA256            string         `json:"base_tree_sha256"`
	VisibleBundleSHA256       string         `json:"visible_bundle_sha256"`
	FinalTreeSHA256           string         `json:"final_tree_sha256"`
	FrozenPatchSHA256         string         `json:"frozen_patch_sha256"`
	ChangedPathRoot           string         `json:"changed_path_root"`
	AuthoringEventRoot        string         `json:"authoring_event_root"`
	AuthoringTranscriptSHA256 string         `json:"authoring_transcript_sha256"`
	AuthoringTranscriptBytes  int64          `json:"authoring_transcript_bytes"`
	ChangedPaths              []string       `json:"changed_paths"`
	Changes                   []FrozenChange `json:"changes"`
	Patch                     []byte         `json:"-"`
	ProtectedPathsIntact      bool           `json:"protected_paths_intact"`
}

// FreezeFailure retains bounded identity when a workspace cannot be frozen.
type FreezeFailure struct {
	Kind                      string `json:"kind"`
	Code                      string `json:"code"`
	BaseTreeSHA256            string `json:"base_tree_sha256"`
	VisibleBundleSHA256       string `json:"visible_bundle_sha256"`
	FinalTreeSHA256           string `json:"final_tree_sha256"`
	ChangedPathRoot           string `json:"changed_path_root"`
	AuthoringEventRoot        string `json:"authoring_event_root"`
	AuthoringTranscriptSHA256 string `json:"authoring_transcript_sha256"`
	AuthoringTranscriptBytes  int64  `json:"authoring_transcript_bytes"`
	ProtectedPathsIntact      bool   `json:"protected_paths_intact"`
}

// FreezeResult is cached so repeated shutdown/freeze paths cannot disagree.
type FreezeResult struct {
	Submission *FrozenSubmission `json:"submission,omitempty"`
	Failure    *FreezeFailure    `json:"failure,omitempty"`
}

// TranscriptIdentity binds the exact newline-delimited canonical event bytes.
type TranscriptIdentity struct {
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
	Events    uint64 `json:"events"`
}

// BundleIdentity is the deterministic identity of one safely materialized
// capsule. Catalog tooling can use it without constructing a runner session.
type BundleIdentity struct {
	VisibleBundleSHA256 string `json:"visible_bundle_sha256"`
	TreeSHA256          string `json:"tree_sha256"`
	Entries             int    `json:"entries"`
	FileBytes           int64  `json:"file_bytes"`
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func safePath(value string, allowDot bool) (string, error) {
	if allowDot && value == "." {
		return value, nil
	}
	if value == "" || len(value) > 256 || !utf8.ValidString(value) || strings.ContainsAny(value, "\\\x00") ||
		strings.HasPrefix(value, "/") || path.Clean(value) != value {
		return "", errors.New("workspace path must be a clean relative POSIX path")
	}
	for _, character := range value {
		if unicode.IsControl(character) {
			return "", errors.New("workspace path contains a control character")
		}
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." || part == ".." || part == ".git" {
			return "", errors.New("workspace path contains a forbidden segment")
		}
	}
	return value, nil
}
