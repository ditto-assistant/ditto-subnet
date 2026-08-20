package codingrunner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

type cachedCall struct {
	requestSHA256 string
	response      ToolResponse
}

// Session owns one task workspace and its authoritative event/freeze state.
// All state transitions are serialized under one lock.
type Session struct {
	mu sync.Mutex

	manifest         Manifest
	executor         CommandExecutor
	root             string
	base             map[string]fileState
	ctx              context.Context
	cancel           context.CancelFunc
	transcript       *os.File
	transcriptPath   string
	transcriptHash   hash.Hash
	transcriptBytes  int64
	replayCacheBytes int64

	editable  map[string]struct{}
	creatable map[string]struct{}
	deletable map[string]struct{}
	tests     map[string]CommandSpec
	builds    map[string]CommandSpec

	sequence    uint64
	eventRoot   string
	calls       map[string]cachedCall
	frozen      *FreezeResult
	latchedKind string
	latchedCode string
	closed      bool
	now         func() time.Time
}

// NewSession verifies and materializes one visible capsule into a private
// temporary workspace. It never exposes the workspace path to the harness.
func NewSession(ctx context.Context, manifest Manifest, visibleBundle io.Reader, executor CommandExecutor) (*Session, error) {
	if ctx == nil {
		return nil, errors.New("coding runner construction context is required")
	}
	now := time.Now()
	if err := manifest.validate(now); err != nil {
		return nil, err
	}
	staged, visibleDigest, err := stageVisibleBundle(ctx, visibleBundle, manifest.Limits.MaxBundleBytes)
	if err != nil {
		return nil, err
	}
	stagedPath := staged.Name()
	defer func() {
		_ = staged.Close()
		_ = os.Remove(stagedPath)
	}()
	if visibleDigest != manifest.VisibleBundleSHA256 {
		return nil, errors.New("visible capsule digest does not match the runner manifest")
	}
	manifest = cloneManifest(manifest)
	root, err := os.MkdirTemp("", "dittobench-coding-runner-")
	if err != nil {
		return nil, fmt.Errorf("create coding workspace: %w", err)
	}
	if err := os.Chmod(root, 0o700); err != nil {
		_ = os.RemoveAll(root)
		return nil, fmt.Errorf("secure coding workspace: %w", err)
	}
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.RemoveAll(root)
		}
	}()
	if err := extractVisibleBundle(ctx, root, staged, manifest.Limits); err != nil {
		return nil, err
	}
	base, err := snapshot(ctx, root, manifest.Limits)
	if err != nil {
		return nil, fmt.Errorf("snapshot visible capsule: %w", err)
	}
	baseTree, err := treeSHA256(base)
	if err != nil {
		return nil, err
	}
	if baseTree != manifest.BaseTreeSHA256 {
		return nil, errors.New("visible capsule base tree does not match the runner manifest")
	}
	for filePath := range listSet(manifest.CreatablePaths) {
		if _, exists := base[filePath]; exists {
			return nil, fmt.Errorf("creatable path already exists in visible capsule: %s", filePath)
		}
	}
	for filePath := range listSet(append(append([]string{}, manifest.EditablePaths...), manifest.DeletablePaths...)) {
		if state, exists := base[filePath]; !exists || state.kind != "file" {
			return nil, fmt.Errorf("editable or deletable path is absent from visible capsule: %s", filePath)
		}
	}
	transcript, err := os.CreateTemp("", ".dittobench-coding-transcript-*")
	if err != nil {
		return nil, fmt.Errorf("create coding transcript: %w", err)
	}
	transcriptPath := transcript.Name()
	if err := transcript.Chmod(0o600); err != nil {
		transcript.Close()
		_ = os.Remove(transcriptPath)
		return nil, err
	}
	sessionContext, cancel := context.WithDeadline(context.Background(), manifest.Deadline)
	session := &Session{
		manifest:       manifest,
		executor:       executor,
		root:           root,
		base:           base,
		ctx:            sessionContext,
		cancel:         cancel,
		transcript:     transcript,
		transcriptPath: transcriptPath,
		transcriptHash: sha256.New(),
		editable:       listSet(manifest.EditablePaths),
		creatable:      listSet(manifest.CreatablePaths),
		deletable:      listSet(manifest.DeletablePaths),
		tests:          commandMap(manifest.TestCommands),
		builds:         commandMap(manifest.BuildCommands),
		eventRoot:      initialEventRoot,
		calls:          make(map[string]cachedCall),
		now:            time.Now,
	}
	cleanup = false
	return session, nil
}

func cloneManifest(manifest Manifest) Manifest {
	manifest.EditablePaths = cloneStrings(manifest.EditablePaths)
	manifest.CreatablePaths = cloneStrings(manifest.CreatablePaths)
	manifest.DeletablePaths = cloneStrings(manifest.DeletablePaths)
	manifest.TestCommands = cloneCommands(manifest.TestCommands)
	manifest.BuildCommands = cloneCommands(manifest.BuildCommands)
	return manifest
}

func cloneCommands(commands []CommandSpec) []CommandSpec {
	result := make([]CommandSpec, len(commands))
	for index, command := range commands {
		result[index] = command
		result[index].Argv = append([]string(nil), command.Argv...)
	}
	return result
}

func listSet(values []string) map[string]struct{} {
	result := make(map[string]struct{}, len(values))
	for _, value := range values {
		result[value] = struct{}{}
	}
	return result
}

func commandMap(values []CommandSpec) map[string]CommandSpec {
	result := make(map[string]CommandSpec, len(values))
	for _, value := range values {
		result[value.ID] = value
	}
	return result
}

// Close destroys the ephemeral workspace. It is safe to call repeatedly.
func (session *Session) Close() error {
	session.cancel()
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.closed {
		return nil
	}
	session.closed = true
	closeErr := session.transcript.Close()
	removeTranscriptErr := os.Remove(session.transcriptPath)
	removeRootErr := os.RemoveAll(session.root)
	return errors.Join(closeErr, ignoreNotExist(removeTranscriptErr), removeRootErr)
}

func ignoreNotExist(err error) error {
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	return err
}

func (session *Session) target(raw string, allowDot bool) (string, string, error) {
	relative, err := safePath(raw, allowDot)
	if err != nil {
		return "", "", err
	}
	if relative == "." {
		return relative, session.root, nil
	}
	current := session.root
	parts := strings.Split(relative, "/")
	for index, part := range parts {
		current = filepath.Join(current, part)
		info, statErr := os.Lstat(current)
		if errors.Is(statErr, fs.ErrNotExist) {
			if index != len(parts)-1 {
				return "", "", errors.New("workspace path has a missing parent")
			}
			break
		}
		if statErr != nil {
			return "", "", errors.New("workspace path is unavailable")
		}
		if info.Mode()&fs.ModeSymlink != 0 {
			return "", "", violation("symlink", "workspace path traverses a symlink")
		}
	}
	return relative, filepath.Join(session.root, filepath.FromSlash(relative)), nil
}

func (session *Session) readFile(ctx context.Context, raw string) (string, string, []byte, fs.FileMode, error) {
	relative, target, err := session.target(raw, false)
	if err != nil {
		return "", "", nil, 0, err
	}
	info, err := os.Lstat(target)
	if err != nil || !info.Mode().IsRegular() {
		return "", "", nil, 0, errors.New("workspace path is not a regular file")
	}
	body, err := readBoundedFile(ctx, target, session.manifest.Limits.MaxFileBytes)
	if err != nil {
		return "", "", nil, 0, errors.New("workspace file is unavailable")
	}
	if !utf8.Valid(body) {
		return "", "", nil, 0, errors.New("workspace file is not UTF-8")
	}
	return relative, string(body), body, info.Mode() & fs.ModePerm, nil
}

func changedPaths(before, after map[string]fileState) []string {
	set := make(map[string]struct{}, len(before)+len(after))
	for filePath := range before {
		set[filePath] = struct{}{}
	}
	for filePath := range after {
		set[filePath] = struct{}{}
	}
	result := make([]string, 0)
	for filePath := range set {
		left, leftOK := before[filePath]
		right, rightOK := after[filePath]
		if !leftOK || !rightOK || left.kind != right.kind || left.mode != right.mode || left.size != right.size || left.sha256 != right.sha256 {
			result = append(result, filePath)
		}
	}
	sort.Strings(result)
	return result
}

func (session *Session) validateChanges(ctx context.Context, current map[string]fileState) ([]string, []FrozenChange, error) {
	paths := changedPaths(session.base, current)
	changes := make([]FrozenChange, 0, len(paths))
	var patchBytes int64
	for _, filePath := range paths {
		before, existed := session.base[filePath]
		after, exists := current[filePath]
		change := FrozenChange{Path: filePath}
		if (existed && before.kind != "file") || (exists && after.kind != "file") {
			return nil, nil, violation("directory_change", "workspace changed directory entry %q", filePath)
		}
		switch {
		case existed && exists:
			if _, allowed := session.editable[filePath]; !allowed {
				return nil, nil, violation("protected_path", "workspace modified protected path %q", filePath)
			}
			if before.mode != after.mode {
				return nil, nil, violation("mode_change", "workspace changed mode for %q", filePath)
			}
			beforeSHA, afterSHA := before.sha256, after.sha256
			change.Kind, change.Mode = "modified", uint32(after.mode)
			change.BeforeSHA256, change.AfterSHA256 = &beforeSHA, &afterSHA
			body, err := readBoundedFile(ctx, filepath.Join(session.root, filepath.FromSlash(filePath)), session.manifest.Limits.MaxFileBytes)
			if err != nil || sha256Hex(body) != afterSHA {
				return nil, nil, errors.New("changed workspace file could not be bound")
			}
			if !utf8.Valid(body) {
				return nil, nil, violation("binary_change", "workspace changed %q to non-UTF-8 content", filePath)
			}
			change.AfterContent = body
			patchBytes += after.size
		case !existed && exists:
			if _, allowed := session.creatable[filePath]; !allowed {
				return nil, nil, violation("undeclared_add", "workspace added undeclared path %q", filePath)
			}
			if after.mode != 0o644 {
				return nil, nil, violation("mode_change", "workspace created %q with a noncanonical mode", filePath)
			}
			afterSHA := after.sha256
			change.Kind, change.Mode = "added", uint32(after.mode)
			change.AfterSHA256 = &afterSHA
			body, err := readBoundedFile(ctx, filepath.Join(session.root, filepath.FromSlash(filePath)), session.manifest.Limits.MaxFileBytes)
			if err != nil || sha256Hex(body) != afterSHA {
				return nil, nil, errors.New("added workspace file could not be bound")
			}
			if !utf8.Valid(body) {
				return nil, nil, violation("binary_change", "workspace created %q with non-UTF-8 content", filePath)
			}
			change.AfterContent = body
			patchBytes += after.size
		case existed && !exists:
			if _, allowed := session.deletable[filePath]; !allowed {
				return nil, nil, violation("undeclared_delete", "workspace deleted undeclared path %q", filePath)
			}
			beforeSHA := before.sha256
			change.Kind, change.Mode, change.BeforeSHA256 = "deleted", uint32(before.mode), &beforeSHA
		}
		if patchBytes > session.manifest.Limits.MaxPatchBytes {
			return nil, nil, violation("patch_limit", "workspace patch exceeds its signed byte limit")
		}
		changes = append(changes, change)
	}
	return paths, changes, nil
}

type patchDocument struct {
	Schema                string         `json:"schema"`
	CodingContractVersion int            `json:"coding_contract_version"`
	CaseID                string         `json:"case_id"`
	BaseTreeSHA256        string         `json:"base_tree_sha256"`
	VisibleBundleSHA256   string         `json:"visible_bundle_sha256"`
	Changes               []FrozenChange `json:"changes"`
}

// Freeze revokes the capability first, then captures one cached authoritative
// result. It never returns a partially mutable session after a failure.
func (session *Session) Freeze() FreezeResult {
	session.cancel()
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.frozen != nil {
		return cloneFreezeResult(*session.frozen)
	}
	if session.closed {
		result := session.freezeFailure("validator_infrastructure", "workspace_closed", nil)
		session.frozen = &result
		return cloneFreezeResult(result)
	}
	result := session.freezeLocked()
	session.frozen = &result
	return cloneFreezeResult(result)
}

func (session *Session) freezeLocked() FreezeResult {
	freezeContext, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	current, err := snapshot(freezeContext, session.root, session.manifest.Limits)
	if err != nil {
		return session.freezeFailure(classifyFreezeError(err), freezeErrorCode(err), nil)
	}
	if session.latchedCode != "" {
		return session.freezeFailure(session.latchedKind, session.latchedCode, current)
	}
	paths, changes, err := session.validateChanges(freezeContext, current)
	if err != nil {
		return session.freezeFailure(classifyFreezeError(err), freezeErrorCode(err), current)
	}
	finalTree, err := treeSHA256(current)
	if err != nil {
		return session.freezeFailure("validator_infrastructure", "tree_digest", current)
	}
	changedRoot, err := pathRoot(paths)
	if err != nil {
		return session.freezeFailure("validator_infrastructure", "changed_path_digest", current)
	}
	patch, err := canonicalStruct(patchDocument{
		Schema:                "dittobench-coding-frozen-patch-v1",
		CodingContractVersion: ContractVersion,
		CaseID:                session.manifest.CaseID,
		BaseTreeSHA256:        session.manifest.BaseTreeSHA256,
		VisibleBundleSHA256:   session.manifest.VisibleBundleSHA256,
		Changes:               changes,
	})
	if err != nil {
		return session.freezeFailure("validator_infrastructure", "patch_encode", current)
	}
	if int64(len(patch)) > session.manifest.Limits.MaxPatchBytes {
		return session.freezeFailure("repair_failure", "patch_limit", current)
	}
	submission := &FrozenSubmission{
		CodingContractVersion:     ContractVersion,
		CaseID:                    session.manifest.CaseID,
		BaseTreeSHA256:            session.manifest.BaseTreeSHA256,
		VisibleBundleSHA256:       session.manifest.VisibleBundleSHA256,
		FinalTreeSHA256:           finalTree,
		FrozenPatchSHA256:         sha256Hex(patch),
		ChangedPathRoot:           changedRoot,
		AuthoringEventRoot:        session.eventRoot,
		AuthoringTranscriptSHA256: session.transcriptIdentityLocked().SHA256,
		AuthoringTranscriptBytes:  session.transcriptBytes,
		ChangedPaths:              cloneStrings(paths),
		Changes:                   cloneChanges(changes),
		Patch:                     cloneBytes(patch),
		ProtectedPathsIntact:      true,
	}
	return FreezeResult{Submission: submission}
}

func (session *Session) freezeFailure(kind, code string, current map[string]fileState) FreezeResult {
	finalTree := strings.Repeat("0", 64)
	changedRoot := strings.Repeat("0", 64)
	if current != nil {
		if digest, err := treeSHA256(current); err == nil {
			finalTree = digest
		}
		if root, err := pathRoot(changedPaths(session.base, current)); err == nil {
			changedRoot = root
		}
	}
	return FreezeResult{Failure: &FreezeFailure{
		Kind:                      kind,
		Code:                      code,
		BaseTreeSHA256:            session.manifest.BaseTreeSHA256,
		VisibleBundleSHA256:       session.manifest.VisibleBundleSHA256,
		FinalTreeSHA256:           finalTree,
		ChangedPathRoot:           changedRoot,
		AuthoringEventRoot:        session.eventRoot,
		AuthoringTranscriptSHA256: session.transcriptIdentityLocked().SHA256,
		AuthoringTranscriptBytes:  session.transcriptBytes,
		ProtectedPathsIntact:      false,
	}}
}

func (session *Session) transcriptIdentityLocked() TranscriptIdentity {
	return TranscriptIdentity{
		SHA256:    hex.EncodeToString(session.transcriptHash.Sum(nil)),
		SizeBytes: session.transcriptBytes,
		Events:    session.sequence,
	}
}

// WriteTranscript streams the exact canonical JSONL event transcript after
// freeze without loading it into memory.
func (session *Session) WriteTranscript(destination io.Writer) (TranscriptIdentity, error) {
	if destination == nil {
		return TranscriptIdentity{}, errors.New("transcript destination is required")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.closed {
		return TranscriptIdentity{}, errSessionClosed
	}
	if session.frozen == nil {
		return TranscriptIdentity{}, errors.New("coding workspace must be frozen before transcript export")
	}
	identity := session.transcriptIdentityLocked()
	written, err := io.Copy(destination, io.NewSectionReader(session.transcript, 0, identity.SizeBytes))
	if err != nil {
		return TranscriptIdentity{}, err
	}
	if written != identity.SizeBytes {
		return TranscriptIdentity{}, errors.New("coding transcript export was incomplete")
	}
	return identity, nil
}

func classifyFreezeError(err error) string {
	var policy *workspaceViolation
	if errors.As(err, &policy) {
		if policy.code == "patch_limit" || policy.code == "workspace_limit" {
			return "repair_failure"
		}
		return "candidate_integrity"
	}
	return "validator_infrastructure"
}

func freezeErrorCode(err error) string {
	var policy *workspaceViolation
	if errors.As(err, &policy) {
		return policy.code
	}
	return "workspace_snapshot"
}

func cloneChanges(values []FrozenChange) []FrozenChange {
	if values == nil {
		return nil
	}
	result := make([]FrozenChange, len(values))
	for index, value := range values {
		result[index] = value
		result[index].AfterContent = cloneBytes(value.AfterContent)
		if value.BeforeSHA256 != nil {
			copyValue := *value.BeforeSHA256
			result[index].BeforeSHA256 = &copyValue
		}
		if value.AfterSHA256 != nil {
			copyValue := *value.AfterSHA256
			result[index].AfterSHA256 = &copyValue
		}
	}
	return result
}

func cloneFreezeResult(value FreezeResult) FreezeResult {
	if value.Failure != nil {
		failure := *value.Failure
		return FreezeResult{Failure: &failure}
	}
	if value.Submission == nil {
		return FreezeResult{}
	}
	submission := *value.Submission
	submission.ChangedPaths = cloneStrings(value.Submission.ChangedPaths)
	submission.Changes = cloneChanges(value.Submission.Changes)
	submission.Patch = cloneBytes(value.Submission.Patch)
	return FreezeResult{Submission: &submission}
}

func cloneStrings(values []string) []string {
	if values == nil {
		return nil
	}
	result := make([]string, len(values))
	copy(result, values)
	return result
}

func cloneBytes(value []byte) []byte {
	if value == nil {
		return nil
	}
	result := make([]byte, len(value))
	copy(result, value)
	return result
}

func marshalResult(value any) (json.RawMessage, error) {
	body, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	return json.RawMessage(body), nil
}
