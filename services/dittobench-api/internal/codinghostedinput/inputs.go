// Package codinghostedinput consumes verified Platform-private authoring frames.
// No frame, catalog record or execution profile may be sent to a miner/validator.
package codinghostedinput

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"reflect"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingseed"
	"github.com/google/uuid"
)

var ErrInput = errors.New("hosted authoring input verification failed")
var roles = []string{"catalog_record", "issue", "visible_bundle", "memory_bundle", "runtime_policy", "resource_profile"}

// Expected is independently supplied by the trusted worker from its committed
// assignment and verified payload descriptor, never inferred from the frame.
type Expected struct {
	EvaluationID           string `json:"evaluation_id"`
	AttemptID              string `json:"attempt_id"`
	WorkerID               string `json:"worker_id"`
	AssignmentSHA256       string `json:"assignment_sha256"`
	RegistrationSHA256     string `json:"registration_sha256"`
	ExecutionProfileSHA256 string `json:"execution_profile_sha256"`
	TaskCommitmentSHA256   string `json:"task_commitment_sha256"`
	DeadlineUnix           int64  `json:"deadline_unix"`
	MaxPatchBytes          int64  `json:"max_patch_bytes"`
	CatalogIndex           int    `json:"catalog_index"`
	CorpusReleaseID        string `json:"corpus_release_id"`
	PrivateReleaseSHA256   string `json:"private_release_sha256"`
}

type object struct {
	Role      string `json:"role"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}
type header struct {
	Expected
	Schema  string   `json:"schema"`
	Objects []object `json:"objects"`
}

// ProfileDigest must be approved into the assignment's execution-profile digest.
// No default or certification fixture image is supplied by this package.
type Profile struct {
	Schema         string                      `json:"schema"`
	ImageDigest    string                      `json:"image_digest"`
	ResourcePolicy codinggrader.ResourcePolicy `json:"resource_policy"`
	Budgets        codingcontract.Budgets      `json:"budgets"`
}

func ProfileDigest(profile Profile) (string, error) {
	if profile.Schema != "dittobench-coding-hosted-authoring-profile-v2" || !strings.HasPrefix(profile.ImageDigest, "sha256:") || !digest(strings.TrimPrefix(profile.ImageDigest, "sha256:")) || profile.ResourcePolicy.Validate() != nil || profile.Budgets.WallTimeSeconds == 0 || profile.Budgets.WallTimeSeconds > 3600 || profile.Budgets.WorkspaceToolCalls != profile.ResourcePolicy.CandidateLimits.MaxToolCalls {
		return "", ErrInput
	}
	body, err := json.Marshal(profile)
	if err != nil {
		return "", ErrInput
	}
	var value map[string]any
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if decoder.Decode(&value) != nil {
		return "", ErrInput
	}
	body, err = json.Marshal(value)
	if err != nil {
		return "", ErrInput
	}
	return sha(append(body, '\n')), nil
}

type Prepared struct {
	mu       sync.Mutex
	used     bool
	closed   bool
	expected Expected
	profile  Profile
	snapshot []byte
	manifest codingrunner.Manifest
	seed     codingseed.HostedProjection
	issue    codingcontract.Issue
	epoch    string
}

// Authority is a private worker projection, not a miner-facing task identity.
// Returning a value prevents the orchestrator from changing prepared authority.
func (p *Prepared) Authority() (Expected, error) {
	if p == nil {
		return Expected{}, ErrInput
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed || !expectedValid(p.expected) {
		return Expected{}, ErrInput
	}
	return p.expected, nil
}

func expectedValid(expected Expected) bool {
	for _, id := range []string{expected.EvaluationID, expected.AttemptID, expected.WorkerID} {
		parsed, err := uuid.Parse(id)
		if err != nil || parsed == uuid.Nil || parsed.String() != id {
			return false
		}
	}
	for _, value := range []string{expected.AssignmentSHA256, expected.RegistrationSHA256, expected.ExecutionProfileSHA256, expected.TaskCommitmentSHA256, expected.PrivateReleaseSHA256} {
		if !digest(value) {
			return false
		}
	}
	remaining := time.Until(time.Unix(expected.DeadlineUnix, 0))
	return remaining > 0 && remaining <= time.Hour && expected.MaxPatchBytes > 0 && expected.MaxPatchBytes <= 128<<20 && expected.CatalogIndex >= 0 && expected.CatalogIndex < 250 && expected.CorpusReleaseID != ""
}

// Validate checks independently supplied authority before consuming a start.
// This is structural validation, not a replacement for Platform ledger checks.
func (expected Expected) Validate() error {
	if !expectedValid(expected) {
		return ErrInput
	}
	return nil
}

// Prepare takes ownership of a bounded private stream and closes it on every
// outcome. Cancellation closes the reader, including a blocked/truncated pipe.
func Prepare(ctx context.Context, expected Expected, profile Profile, source io.ReadCloser) (*Prepared, error) {
	if ctx == nil || source == nil || (reflect.ValueOf(source).Kind() == reflect.Pointer && reflect.ValueOf(source).IsNil()) {
		return nil, ErrInput
	}
	defer source.Close()
	if ctx.Err() != nil || !expectedValid(expected) {
		return nil, ErrInput
	}
	profileSHA, err := ProfileDigest(profile)
	if err != nil || profileSHA != expected.ExecutionProfileSHA256 {
		return nil, ErrInput
	}
	call, cancel := context.WithDeadline(ctx, time.Unix(expected.DeadlineUnix, 0))
	defer cancel()
	call, stop := context.WithTimeout(call, 180*time.Second)
	defer stop()
	after := context.AfterFunc(call, func() { _ = source.Close() })
	defer after()
	var prefix [4]byte
	if _, err := io.ReadFull(source, prefix[:]); err != nil {
		return nil, ErrInput
	}
	n := binary.BigEndian.Uint32(prefix[:])
	if n == 0 || n > 16384 {
		return nil, ErrInput
	}
	body := make([]byte, int(n))
	if _, err := io.ReadFull(source, body); err != nil || codingcontract.RequireExactCanonicalJSON(body) != nil {
		return nil, ErrInput
	}
	var h header
	if json.Unmarshal(body, &h) != nil || h.Schema != "dittobench-coding-hosted-authoring-inputs-v2" || h.Expected != expected || len(h.Objects) != len(roles) {
		return nil, ErrInput
	}
	objects := make(map[string][]byte, len(roles))
	for i, item := range h.Objects {
		limit := int64(4 << 20)
		if item.Role == "visible_bundle" {
			limit = min(128<<20, profile.ResourcePolicy.CandidateLimits.MaxBundleBytes)
		}
		if item.Role == "catalog_record" {
			limit = 64 << 10
		}
		if item.Role != roles[i] || !digest(item.SHA256) || item.SizeBytes <= 0 || item.SizeBytes > limit || call.Err() != nil {
			return nil, ErrInput
		}
		value := make([]byte, int(item.SizeBytes))
		if _, err := io.ReadFull(source, value); err != nil || sha(value) != item.SHA256 {
			return nil, ErrInput
		}
		objects[item.Role] = value
	}
	marker := make([]byte, len("DITTO-AUTHORING-READY-V2\n"))
	if _, err := io.ReadFull(source, marker); err != nil || string(marker) != "DITTO-AUTHORING-READY-V2\n" {
		return nil, ErrInput
	}
	var extra [1]byte
	if n, err := source.Read(extra[:]); n != 0 || err != io.EOF || call.Err() != nil {
		return nil, ErrInput
	}
	prepared, err := compile(call, expected, profile, objects)
	if err != nil || call.Err() != nil {
		return nil, ErrInput
	}
	return prepared, nil
}

type command struct {
	ID                  string            `json:"id"`
	Argv                []string          `json:"argv"`
	Environment         map[string]string `json:"environment"`
	TimeoutMilliseconds int64             `json:"timeout_milliseconds"`
}
type runtimePolicy struct {
	Schema    string    `json:"schema"`
	Platform  string    `json:"environment_platform"`
	Network   string    `json:"network"`
	Editable  []string  `json:"editable_paths"`
	Creatable []string  `json:"creatable_paths"`
	Deletable []string  `json:"deletable_paths"`
	Tests     []command `json:"test_commands"`
	Builds    []command `json:"build_commands"`
}
type resourceProfile struct {
	Schema  string `json:"schema"`
	CPUs    uint32 `json:"cpus_milli"`
	Memory  uint64 `json:"memory_mib"`
	Disk    uint64 `json:"disk_mib"`
	Pids    uint32 `json:"pids"`
	Wall    uint64 `json:"wall_time_seconds"`
	Network string `json:"network"`
}

func compile(ctx context.Context, expected Expected, profile Profile, objects map[string][]byte) (*Prepared, error) {
	var task codingcontract.PrivateCatalogTaskV2
	if codingcontract.RequireExactCanonicalJSON(objects["catalog_record"]) != nil || json.Unmarshal(objects["catalog_record"], &task) != nil || task.Validate() != nil || task.TaskCommitmentSHA256 != expected.TaskCommitmentSHA256 || int(task.CatalogIndex) != expected.CatalogIndex || task.PrivateReleaseSHA256 != expected.PrivateReleaseSHA256 || task.CorpusReleaseID != expected.CorpusReleaseID {
		return nil, ErrInput
	}
	for role, want := range map[string]string{"issue": task.VisibleIssueSHA256, "memory_bundle": task.MemoryBundleSHA256, "runtime_policy": task.RuntimePolicySHA256, "resource_profile": task.ResourceProfileSHA256} {
		if sha(objects[role]) != want || codingcontract.RequireExactCanonicalJSON(objects[role]) != nil {
			return nil, ErrInput
		}
	}
	var policy runtimePolicy
	var resource resourceProfile
	if json.Unmarshal(objects["runtime_policy"], &policy) != nil || json.Unmarshal(objects["resource_profile"], &resource) != nil || policy.Schema != "dittobench-coding-private-runtime-policy-v2" || policy.Network != "none" || policy.Platform != "linux/amd64" || resource.Schema != "dittobench-coding-private-resource-profile-v2" || resource.Network != "none" || resource.Memory == 0 || resource.Memory > 1<<20 || resource.Disk == 0 || resource.Disk > 1<<20 || resource.Wall == 0 || resource.Wall > 3600 {
		return nil, ErrInput
	}
	limits := profile.ResourcePolicy.CandidateLimits
	if resource.CPUs != profile.ResourcePolicy.CPUQuotaMillis || resource.Pids != profile.ResourcePolicy.PidsLimit || resource.Memory<<20 != profile.ResourcePolicy.MemoryLimitBytes || resource.Disk<<20 != profile.ResourcePolicy.ScratchLimitBytes || profile.Budgets.WallTimeSeconds > resource.Wall || limits.MaxPatchBytes > expected.MaxPatchBytes {
		return nil, ErrInput
	}
	snapshot, err := codingrunner.CompileHostedSnapshot(ctx, objects["visible_bundle"], sha(objects["visible_bundle"]), limits)
	if err != nil || snapshot.CapsuleTreeSHA256 != task.VisibleSnapshotTreeSHA256 {
		return nil, ErrInput
	}
	manifest := codingrunner.Manifest{CodingContractVersion: 2, TicketID: expected.EvaluationID, CaseID: expected.AttemptID, ProfileCapabilityID: "hosted-" + expected.AttemptID, VisibleBundleSHA256: snapshot.Identity.VisibleBundleSHA256, BaseTreeSHA256: snapshot.Identity.TreeSHA256, Deadline: time.Unix(expected.DeadlineUnix, 0), EditablePaths: policy.Editable, CreatablePaths: policy.Creatable, DeletablePaths: policy.Deletable, Limits: limits}
	manifest.TestCommands, err = commands(policy.Tests)
	if err != nil {
		return nil, ErrInput
	}
	manifest.BuildCommands, err = commands(policy.Builds)
	if err != nil {
		return nil, ErrInput
	}
	authority := codingrunner.HostedAuthority{EvaluationID: expected.EvaluationID, AttemptID: expected.AttemptID, AssignmentSHA256: expected.AssignmentSHA256}
	if codingrunner.ValidateHostedManifest(authority, manifest, time.Now()) != nil {
		return nil, ErrInput
	}
	projector, err := codingseed.New(codingseed.Config{MaxBundleBytes: 4 << 20, SeedTimeout: time.Minute})
	if err != nil {
		return nil, ErrInput
	}
	seed, err := projector.ProjectHosted(bytes.NewReader(objects["memory_bundle"]), codingseed.Binding{TicketID: manifest.TicketID, CaseID: manifest.CaseID, ProfileCapabilityID: manifest.ProfileCapabilityID, MemoryBundleSHA256: task.MemoryBundleSHA256, Deadline: manifest.Deadline})
	if err != nil {
		return nil, ErrInput
	}
	var visibleIssue struct {
		Schema string `json:"schema"`
		codingcontract.Issue
	}
	if json.Unmarshal(objects["issue"], &visibleIssue) != nil || visibleIssue.Schema != "dittobench-coding-private-visible-issue-v2" {
		return nil, ErrInput
	}
	prepared := &Prepared{expected: expected, profile: profile, snapshot: snapshot.Bundle, manifest: manifest, seed: seed, issue: visibleIssue.Issue, epoch: task.RepositoryEpoch}
	if _, err := prepared.RunRequest("http://workspace.invalid/tool", "http://inference.invalid"); err != nil {
		return nil, ErrInput
	}
	return prepared, nil
}

func commands(values []command) ([]codingrunner.CommandSpec, error) {
	if values == nil {
		return nil, ErrInput
	}
	out := make([]codingrunner.CommandSpec, 0, len(values))
	for _, value := range values {
		if value.Environment == nil || len(value.Environment) != 0 || value.TimeoutMilliseconds <= 0 || value.TimeoutMilliseconds > 600000 {
			return nil, ErrInput
		}
		spec := codingrunner.CommandSpec{ID: value.ID, Argv: value.Argv, Timeout: time.Duration(value.TimeoutMilliseconds) * time.Millisecond}
		if spec.Validate() != nil {
			return nil, ErrInput
		}
		out = append(out, spec)
	}
	return out, nil
}

func (p *Prepared) SeedRequest() (codingcontract.HostedSeedRequest, error) {
	if p == nil {
		return codingcontract.HostedSeedRequest{}, ErrInput
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed || !expectedValid(p.expected) {
		return codingcontract.HostedSeedRequest{}, ErrInput
	}
	return p.seed.Request(), nil
}
func (p *Prepared) RunRequest(workspaceURL, inferenceURL string) (codingcontract.HostedRunRequest, error) {
	if p == nil || !expectedValid(p.expected) {
		return codingcontract.HostedRunRequest{}, ErrInput
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return codingcontract.HostedRunRequest{}, ErrInput
	}
	policy := codingcontract.RuntimePolicy{EditablePaths: append([]string{}, p.manifest.EditablePaths...), TestCommandIDs: []string{}, BuildCommandIDs: []string{}}
	for _, c := range p.manifest.TestCommands {
		policy.TestCommandIDs = append(policy.TestCommandIDs, c.ID)
	}
	for _, c := range p.manifest.BuildCommands {
		policy.BuildCommandIDs = append(policy.BuildCommandIDs, c.ID)
	}
	issue := p.issue
	issue.Constraints = append([]string{}, issue.Constraints...)
	run := codingcontract.HostedRunRequest{CodingContractVersion: 2, TicketID: p.expected.EvaluationID, CaseID: p.expected.AttemptID, ProfileCapabilityID: p.manifest.ProfileCapabilityID, RepositoryEpoch: p.epoch, VisibleBundleSHA256: p.manifest.VisibleBundleSHA256, Issue: issue, RuntimePolicy: policy, WorkspaceCapabilityURL: workspaceURL, InferenceBaseURL: inferenceURL, Budgets: p.profile.Budgets}
	if run.Validate() != nil {
		return codingcontract.HostedRunRequest{}, ErrInput
	}
	return run, nil
}

// Begin creates the resource-bound authoring executor and native workspace once.
// The worker must still enforce grant freshness and revoke/drain before grading.
func (p *Prepared) Begin(ctx context.Context, factory *codingexecutor.PhaseFactory) (*codingrunner.Session, error) {
	if p == nil || factory == nil || ctx == nil || ctx.Err() != nil || !expectedValid(p.expected) {
		return nil, ErrInput
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.used || p.closed {
		return nil, ErrInput
	}
	p.used = true
	setup, cancel := context.WithDeadline(ctx, p.manifest.Deadline)
	defer cancel()
	executor, err := factory.Authoring(setup, p.profile.ImageDigest, p.profile.ResourcePolicy)
	if err != nil {
		return nil, ErrInput
	}
	session, err := codingrunner.NewHostedSession(setup, codingrunner.HostedAuthority{EvaluationID: p.expected.EvaluationID, AttemptID: p.expected.AttemptID, AssignmentSHA256: p.expected.AssignmentSHA256}, p.manifest, bytes.NewReader(p.snapshot), executor)
	if err != nil || setup.Err() != nil {
		if session != nil {
			_ = session.Close()
		}
		return nil, ErrInput
	}
	return session, nil
}

// Close drops prepared plaintext; it does not close an already-created workspace
// or erase copies already delivered to the trusted worker or candidate.
func (p *Prepared) Close() {
	if p == nil {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	clear(p.snapshot)
	p.snapshot = nil
	p.seed = codingseed.HostedProjection{}
	p.issue = codingcontract.Issue{}
	p.closed = true
}

func digest(value string) bool {
	if len(value) != 64 {
		return false
	}
	for _, c := range value {
		if !strings.ContainsRune("0123456789abcdef", c) {
			return false
		}
	}
	return true
}
func sha(body []byte) string                   { return fmt.Sprintf("%x", sha256.Sum256(body)) }
func (*Prepared) MarshalJSON() ([]byte, error) { return nil, ErrInput }
func (*Prepared) String() string               { return "HostedAuthoringPrepared{private}" }
func (p *Prepared) GoString() string           { return p.String() }
func (p *Prepared) LogValue() slog.Value       { return slog.StringValue(p.String()) }
