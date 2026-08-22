package codingrunner

import (
	"bytes"
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"unicode/utf8"
)

// ReplayWorkspace is a fresh trusted workspace containing one verified frozen
// submission. Its path may be given only to the later trusted grader executor.
type ReplayWorkspace struct {
	mu     sync.Mutex
	root   string
	limits Limits
	remove func(string) error
	closed bool
}

// ProtectedBundle is a separately materialized grader artifact. Candidate
// processes must never receive its path or mount.
type ProtectedBundle struct {
	mu     sync.Mutex
	root   string
	limits Limits
	tree   string
	remove func(string) error
	closed bool
}

// InspectBundle streams, safely materializes, and identifies one capsule.
func InspectBundle(ctx context.Context, bundle io.Reader, limits Limits) (BundleIdentity, error) {
	if ctx == nil {
		return BundleIdentity{}, errors.New("bundle inspection context is required")
	}
	if err := limits.Validate(); err != nil {
		return BundleIdentity{}, err
	}
	staged, digest, err := stageVisibleBundle(ctx, bundle, limits.MaxBundleBytes)
	if err != nil {
		return BundleIdentity{}, err
	}
	stagedPath := staged.Name()
	defer func() {
		_ = staged.Close()
		_ = os.Remove(stagedPath)
	}()
	root, err := os.MkdirTemp("", "dittobench-bundle-inspect-")
	if err != nil {
		return BundleIdentity{}, err
	}
	defer os.RemoveAll(root)
	if err := os.Chmod(root, 0o700); err != nil {
		return BundleIdentity{}, err
	}
	if err := extractVisibleBundle(ctx, root, staged, limits); err != nil {
		return BundleIdentity{}, err
	}
	state, err := snapshot(ctx, root, limits)
	if err != nil {
		return BundleIdentity{}, err
	}
	tree, err := treeSHA256(state)
	if err != nil {
		return BundleIdentity{}, err
	}
	identity := BundleIdentity{VisibleBundleSHA256: digest, TreeSHA256: tree, Entries: len(state)}
	for _, entry := range state {
		if entry.kind == "file" {
			identity.FileBytes += entry.size
		}
	}
	return identity, nil
}

// ReplayFrozenSubmission reconstructs a fresh visible base, verifies every
// frozen identity, and applies exact full-file transitions. It never accepts a
// fuzzy diff or a miner-provided workspace.
func ReplayFrozenSubmission(
	ctx context.Context,
	submission FrozenSubmission,
	visibleBundle io.Reader,
	limits Limits,
) (*ReplayWorkspace, error) {
	if ctx == nil {
		return nil, errors.New("frozen replay context is required")
	}
	if err := limits.Validate(); err != nil {
		return nil, err
	}
	cloned := cloneFreezeResult(FreezeResult{Submission: &submission})
	if cloned.Submission == nil {
		return nil, errors.New("frozen submission is unavailable")
	}
	submission = *cloned.Submission
	if err := validateFrozenSubmission(submission, limits); err != nil {
		return nil, err
	}
	staged, digest, err := stageVisibleBundle(ctx, visibleBundle, limits.MaxBundleBytes)
	if err != nil {
		return nil, err
	}
	stagedPath := staged.Name()
	defer func() {
		_ = staged.Close()
		_ = os.Remove(stagedPath)
	}()
	if digest != submission.VisibleBundleSHA256 {
		return nil, errors.New("frozen replay visible capsule digest mismatch")
	}
	root, err := os.MkdirTemp("", "dittobench-coding-replay-")
	if err != nil {
		return nil, err
	}
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.RemoveAll(root)
		}
	}()
	if err := os.Chmod(root, 0o700); err != nil {
		return nil, err
	}
	if err := extractVisibleBundle(ctx, root, staged, limits); err != nil {
		return nil, err
	}
	base, err := snapshot(ctx, root, limits)
	if err != nil {
		return nil, err
	}
	baseTree, err := treeSHA256(base)
	if err != nil {
		return nil, err
	}
	if baseTree != submission.BaseTreeSHA256 {
		return nil, errors.New("frozen replay base tree mismatch")
	}
	for _, change := range submission.Changes {
		if err := applyFrozenChange(ctx, root, base, change, limits); err != nil {
			return nil, err
		}
	}
	final, err := snapshot(ctx, root, limits)
	if err != nil {
		return nil, err
	}
	finalTree, err := treeSHA256(final)
	if err != nil {
		return nil, err
	}
	if finalTree != submission.FinalTreeSHA256 {
		return nil, errors.New("frozen replay final tree mismatch")
	}
	paths := changedPaths(base, final)
	if !slices.Equal(paths, submission.ChangedPaths) {
		return nil, errors.New("frozen replay changed paths mismatch")
	}
	rootDigest, err := pathRoot(paths)
	if err != nil {
		return nil, err
	}
	if rootDigest != submission.ChangedPathRoot {
		return nil, errors.New("frozen replay changed-path root mismatch")
	}
	cleanup = false
	return &ReplayWorkspace{root: root, limits: limits, remove: os.RemoveAll}, nil
}

func validateFrozenSubmission(submission FrozenSubmission, limits Limits) error {
	if submission.CodingContractVersion != ContractVersion || !validIdentifier(submission.CaseID, 256) ||
		!isLowerSHA256(submission.VisibleBundleSHA256) || !isLowerSHA256(submission.BaseTreeSHA256) ||
		!isLowerSHA256(submission.FinalTreeSHA256) || !isLowerSHA256(submission.FrozenPatchSHA256) ||
		!isLowerSHA256(submission.ChangedPathRoot) || !isLowerSHA256(submission.AuthoringEventRoot) ||
		!isLowerSHA256(submission.AuthoringTranscriptSHA256) || submission.AuthoringTranscriptBytes < 0 ||
		submission.AuthoringTranscriptBytes > limits.MaxTranscriptBytes || int64(len(submission.Patch)) > limits.MaxPatchBytes ||
		!submission.ProtectedPathsIntact || submission.ChangedPaths == nil || submission.Changes == nil {
		return errors.New("frozen submission identity is invalid")
	}
	if !slices.IsSorted(submission.ChangedPaths) || len(submission.ChangedPaths) != len(submission.Changes) ||
		len(submission.ChangedPaths) > limits.MaxEntries {
		return errors.New("frozen submission paths are not canonical")
	}
	seen := make(map[string]struct{}, len(submission.Changes))
	var afterBytes int64
	for index, change := range submission.Changes {
		if change.Path != submission.ChangedPaths[index] {
			return errors.New("frozen submission changes do not match changed paths")
		}
		if _, duplicate := seen[change.Path]; duplicate {
			return errors.New("frozen submission repeats a changed path")
		}
		seen[change.Path] = struct{}{}
		if _, err := safePath(change.Path, false); err != nil {
			return err
		}
		if change.Mode&^0o777 != 0 || change.Mode&0o400 == 0 || change.Mode&0o022 != 0 {
			return errors.New("frozen submission contains an unsafe file mode")
		}
		switch change.Kind {
		case "added":
			if change.BeforeSHA256 != nil || change.AfterSHA256 == nil || !isLowerSHA256(*change.AfterSHA256) ||
				int64(len(change.AfterContent)) > limits.MaxFileBytes || change.Mode != 0o644 || !utf8.Valid(change.AfterContent) {
				return errors.New("frozen added-file transition is invalid")
			}
		case "modified":
			if change.BeforeSHA256 == nil || change.AfterSHA256 == nil || !isLowerSHA256(*change.BeforeSHA256) ||
				!isLowerSHA256(*change.AfterSHA256) || int64(len(change.AfterContent)) > limits.MaxFileBytes ||
				!utf8.Valid(change.AfterContent) {
				return errors.New("frozen modified-file transition is invalid")
			}
		case "deleted":
			if change.BeforeSHA256 == nil || !isLowerSHA256(*change.BeforeSHA256) || change.AfterSHA256 != nil || change.AfterContent != nil {
				return errors.New("frozen deleted-file transition is invalid")
			}
		default:
			return errors.New("frozen submission contains an unknown transition")
		}
		if change.AfterSHA256 != nil && sha256Hex(change.AfterContent) != *change.AfterSHA256 {
			return errors.New("frozen after-content digest mismatch")
		}
		if int64(len(change.AfterContent)) > limits.MaxPatchBytes-afterBytes {
			return errors.New("frozen transition bytes exceed patch limit")
		}
		afterBytes += int64(len(change.AfterContent))
	}
	changedRoot, err := pathRoot(submission.ChangedPaths)
	if err != nil || changedRoot != submission.ChangedPathRoot {
		return errors.New("frozen changed-path root mismatch")
	}
	patch, err := canonicalStruct(patchDocument{
		Schema:                "dittobench-coding-frozen-patch-v1",
		CodingContractVersion: ContractVersion,
		CaseID:                submission.CaseID,
		BaseTreeSHA256:        submission.BaseTreeSHA256,
		VisibleBundleSHA256:   submission.VisibleBundleSHA256,
		Changes:               submission.Changes,
	})
	if err != nil {
		return err
	}
	if int64(len(patch)) > limits.MaxPatchBytes || sha256Hex(patch) != submission.FrozenPatchSHA256 {
		return errors.New("frozen patch digest mismatch")
	}
	if len(submission.Patch) != 0 && !bytes.Equal(submission.Patch, patch) {
		return errors.New("frozen patch bytes mismatch")
	}
	return nil
}

func applyFrozenChange(
	ctx context.Context,
	root string,
	base map[string]fileState,
	change FrozenChange,
	limits Limits,
) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	target := filepath.Join(root, filepath.FromSlash(change.Path))
	before, existed := base[change.Path]
	switch change.Kind {
	case "added":
		if existed {
			return errors.New("frozen add collides with visible base")
		}
		parentRelative := filepath.ToSlash(filepath.Dir(filepath.FromSlash(change.Path)))
		if err := mkdirAllExact(root, parentRelative); err != nil {
			return err
		}
		handle, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, fs.FileMode(change.Mode))
		if err != nil {
			return err
		}
		if err := handle.Chmod(fs.FileMode(change.Mode)); err != nil {
			handle.Close()
			return err
		}
		written, err := handle.Write(change.AfterContent)
		closeErr := handle.Close()
		if err != nil || written != len(change.AfterContent) || closeErr != nil {
			return errors.New("frozen added file could not be written")
		}
	case "modified":
		if !existed || before.kind != "file" || before.sha256 != *change.BeforeSHA256 || uint32(before.mode) != change.Mode {
			return errors.New("frozen modification does not match visible base")
		}
		if err := atomicWrite(target, change.AfterContent, fs.FileMode(change.Mode)); err != nil {
			return err
		}
	case "deleted":
		if !existed || before.kind != "file" || before.sha256 != *change.BeforeSHA256 || uint32(before.mode) != change.Mode {
			return errors.New("frozen deletion does not match visible base")
		}
		if err := os.Remove(target); err != nil {
			return err
		}
	}
	return nil
}

// TrustedPath returns the validator-local path for an injected trusted grader
// executor. It must never be returned to the miner or used as its direct cwd.
func (workspace *ReplayWorkspace) TrustedPath() (string, error) {
	workspace.mu.Lock()
	defer workspace.mu.Unlock()
	if workspace.closed {
		return "", errors.New("replay workspace is closed")
	}
	return workspace.root, nil
}

// TreeSHA256 recomputes the complete current workspace tree identity.
func (workspace *ReplayWorkspace) TreeSHA256(ctx context.Context) (string, error) {
	workspace.mu.Lock()
	defer workspace.mu.Unlock()
	if workspace.closed {
		return "", errors.New("replay workspace is closed")
	}
	state, err := snapshot(ctx, workspace.root, workspace.limits)
	if err != nil {
		return "", err
	}
	return treeSHA256(state)
}

// MaterializeProtectedBundle securely reconstructs a grader bundle outside the
// candidate workspace. The later executor must expose it only to its trusted
// test supervisor, never to candidate code.
func (workspace *ReplayWorkspace) MaterializeProtectedBundle(
	ctx context.Context,
	expectedSHA256 string,
	bundle io.Reader,
	limits Limits,
) (*ProtectedBundle, error) {
	workspace.mu.Lock()
	defer workspace.mu.Unlock()
	if workspace.closed {
		return nil, errors.New("replay workspace is closed")
	}
	if !isLowerSHA256(expectedSHA256) {
		return nil, errors.New("grader bundle digest is invalid")
	}
	if err := limits.Validate(); err != nil {
		return nil, err
	}
	staged, digest, err := stageVisibleBundle(ctx, bundle, limits.MaxBundleBytes)
	if err != nil {
		return nil, err
	}
	stagedPath := staged.Name()
	defer func() {
		_ = staged.Close()
		_ = os.Remove(stagedPath)
	}()
	if digest != expectedSHA256 {
		return nil, errors.New("grader bundle digest mismatch")
	}
	root, err := os.MkdirTemp("", "dittobench-protected-grader-")
	if err != nil {
		return nil, err
	}
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.RemoveAll(root)
		}
	}()
	if err := os.Chmod(root, 0o700); err != nil {
		return nil, err
	}
	if err := extractVisibleBundle(ctx, root, staged, limits); err != nil {
		return nil, err
	}
	state, err := snapshot(ctx, root, limits)
	if err != nil {
		return nil, err
	}
	tree, err := treeSHA256(state)
	if err != nil {
		return nil, err
	}
	cleanup = false
	return &ProtectedBundle{root: root, limits: limits, tree: tree, remove: os.RemoveAll}, nil
}

// TrustedPath returns the protected path for the trusted test supervisor only.
func (bundle *ProtectedBundle) TrustedPath() (string, error) {
	bundle.mu.Lock()
	defer bundle.mu.Unlock()
	if bundle.closed {
		return "", errors.New("protected grader bundle is closed")
	}
	return bundle.root, nil
}

// TreeSHA256 recomputes the protected grader tree identity.
func (bundle *ProtectedBundle) TreeSHA256(ctx context.Context) (string, error) {
	bundle.mu.Lock()
	defer bundle.mu.Unlock()
	if bundle.closed {
		return "", errors.New("protected grader bundle is closed")
	}
	state, err := snapshot(ctx, bundle.root, bundle.limits)
	if err != nil {
		return "", err
	}
	return treeSHA256(state)
}

// InitialTreeSHA256 returns the verified tree at materialization.
func (bundle *ProtectedBundle) InitialTreeSHA256() string {
	bundle.mu.Lock()
	defer bundle.mu.Unlock()
	return bundle.tree
}

// Close destroys the protected grader bundle.
func (bundle *ProtectedBundle) Close() error {
	bundle.mu.Lock()
	defer bundle.mu.Unlock()
	if bundle.closed {
		return nil
	}
	if err := removeAllWithRetries(bundle.remove, bundle.root); err != nil {
		return err
	}
	bundle.closed = true
	bundle.root = ""
	return nil
}

// Close destroys the pristine grader workspace.
func (workspace *ReplayWorkspace) Close() error {
	workspace.mu.Lock()
	defer workspace.mu.Unlock()
	if workspace.closed {
		return nil
	}
	if err := removeAllWithRetries(workspace.remove, workspace.root); err != nil {
		return err
	}
	workspace.closed = true
	workspace.root = ""
	return nil
}

func removeAllWithRetries(remove func(string) error, target string) error {
	if remove == nil {
		return errors.New("cleanup function is unavailable")
	}
	var last error
	for range 3 {
		if err := remove(target); err == nil {
			return nil
		} else {
			last = err
		}
	}
	return last
}
