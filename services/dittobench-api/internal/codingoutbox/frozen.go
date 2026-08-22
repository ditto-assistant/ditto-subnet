package codingoutbox

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"golang.org/x/sys/unix"
)

type frozenPatchDocument struct {
	Schema                string                      `json:"schema"`
	CodingContractVersion int                         `json:"coding_contract_version"`
	CaseID                string                      `json:"case_id"`
	BaseTreeSHA256        string                      `json:"base_tree_sha256"`
	VisibleBundleSHA256   string                      `json:"visible_bundle_sha256"`
	Changes               []codingrunner.FrozenChange `json:"changes"`
}

func (attempt *Attempt) StoreFrozen(
	ctx context.Context,
	submission codingrunner.FrozenSubmission,
) (FrozenArtifact, error) {
	if attempt == nil || attempt.store == nil || ctx == nil || ctx.Err() != nil || len(submission.Patch) == 0 {
		return FrozenArtifact{}, ErrInvalid
	}
	store := attempt.store
	store.mu.Lock()
	defer store.mu.Unlock()
	now := store.config.Now().UTC()
	if err := store.checkOpenAndClock(now); err != nil {
		return FrozenArtifact{}, err
	}
	record, err := store.recordForAttempt(attempt.id)
	if err != nil {
		return FrozenArtifact{}, err
	}
	if err := validateSubmissionForRecord(submission, record); err != nil {
		return FrozenArtifact{}, err
	}
	metadata := metadataFromSubmission(submission)
	artifact := FrozenArtifact{
		ObjectKey:         "sha256/" + submission.FrozenPatchSHA256,
		FrozenPatchSHA256: submission.FrozenPatchSHA256, SizeBytes: int64(len(submission.Patch)),
		FinalTreeSHA256: submission.FinalTreeSHA256, ChangedPathRoot: submission.ChangedPathRoot,
	}
	if record.Frozen != nil {
		if record.Frozen.Artifact != artifact || !equalMetadata(record.Frozen.Metadata, metadata) {
			return FrozenArtifact{}, ErrConflict
		}
		if err := store.verifyReference(artifact.ObjectKey, artifact.FrozenPatchSHA256, artifact.SizeBytes); err != nil {
			return FrozenArtifact{}, err
		}
		return artifact, nil
	}
	if !record.Binding.Deadline.Add(store.config.FinalizationGrace).After(now) {
		return FrozenArtifact{}, ErrState
	}
	if record.State != StateCollecting || record.WriterNonce != "" || record.Transcript == nil {
		return FrozenArtifact{}, ErrState
	}
	if !store.capacityHealthy() {
		return FrozenArtifact{}, ErrCapacity
	}
	file, name, dev, ino, err := newStagingFile(store.dirs.staging, "frozen-")
	if err != nil {
		return FrozenArtifact{}, err
	}
	keep := false
	defer func() {
		_ = file.Close()
		if !keep {
			mode := uint32(0o600)
			if verifyNamedInode(store.dirs.staging, name, dev, ino, 0o400) == nil {
				mode = 0o400
			}
			if verifyNamedInode(store.dirs.staging, name, dev, ino, mode) == nil {
				_ = unix.Unlinkat(int(store.dirs.staging.Fd()), name, 0)
			}
		}
	}()
	if err := writeAll(file, submission.Patch); err != nil || file.Sync() != nil || file.Chmod(0o400) != nil ||
		file.Sync() != nil || file.Close() != nil {
		return FrozenArtifact{}, errors.New("persist frozen submission bytes")
	}
	if err := store.installObject(name, dev, ino, artifact.FrozenPatchSHA256, artifact.SizeBytes); err != nil {
		return FrozenArtifact{}, err
	}
	keep = true
	updated := cloneRecord(record)
	updated.Generation++
	updated.Frozen = &FrozenRecord{Artifact: artifact, Metadata: metadata}
	result := codingrunner.FreezeResult{Submission: &submission}
	updated.OutcomeSHA256, err = digestJSON(result)
	if err != nil {
		return FrozenArtifact{}, err
	}
	updated.State = StateReady
	updated.SealedAtUnix = now.Unix()
	if err := store.persistRecord(updated); err != nil {
		return FrozenArtifact{}, err
	}
	store.records[attempt.id] = updated
	return artifact, nil
}

func (attempt *Attempt) Seal(ctx context.Context, result codingrunner.FreezeResult) (Record, error) {
	if attempt == nil || attempt.store == nil || ctx == nil || ctx.Err() != nil ||
		(result.Submission == nil) == (result.Failure == nil) {
		return Record{}, ErrInvalid
	}
	outcomeSHA, err := digestJSON(result)
	if err != nil {
		return Record{}, err
	}
	store := attempt.store
	store.mu.Lock()
	defer store.mu.Unlock()
	now := store.config.Now().UTC()
	if err := store.checkOpenAndClock(now); err != nil {
		return Record{}, err
	}
	record, err := store.recordForAttempt(attempt.id)
	if err != nil {
		return Record{}, err
	}
	if record.State == StateReady || record.State == StateTerminalWithoutPatch || record.State == StateReleased {
		if record.OutcomeSHA256 == outcomeSHA {
			return *cloneRecord(record), nil
		}
		return Record{}, ErrConflict
	}
	if !record.Binding.Deadline.Add(store.config.FinalizationGrace).After(now) {
		return Record{}, ErrState
	}
	if record.State != StateCollecting || record.WriterNonce != "" || record.Transcript == nil {
		return Record{}, ErrState
	}
	updated := cloneRecord(record)
	if result.Submission != nil {
		if updated.Frozen == nil || validateSubmissionForRecord(*result.Submission, updated) != nil ||
			updated.Frozen.Artifact.FrozenPatchSHA256 != result.Submission.FrozenPatchSHA256 ||
			!equalMetadata(updated.Frozen.Metadata, metadataFromSubmission(*result.Submission)) {
			return Record{}, ErrConflict
		}
		updated.State = StateReady
	} else {
		failure := result.Failure
		if updated.Frozen != nil || !validFailure(*failure) || failure.AuthoringTranscriptSHA256 != updated.Transcript.SHA256 ||
			failure.AuthoringTranscriptBytes != updated.Transcript.SizeBytes {
			return Record{}, ErrConflict
		}
		updated.State = StateTerminalWithoutPatch
		failureCopy := *failure
		updated.Failure = &failureCopy
	}
	updated.Generation++
	updated.OutcomeSHA256 = outcomeSHA
	updated.SealedAtUnix = now.Unix()
	if err := store.persistRecord(updated); err != nil {
		return Record{}, err
	}
	store.records[attempt.id] = updated
	return *cloneRecord(updated), nil
}

func (store *Store) LoadFrozen(ctx context.Context, id string) (codingrunner.FrozenSubmission, error) {
	if ctx == nil || ctx.Err() != nil || !lowerSHA256(id) {
		return codingrunner.FrozenSubmission{}, ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(store.config.Now().UTC()); err != nil {
		return codingrunner.FrozenSubmission{}, err
	}
	record := store.records[id]
	if record == nil || record.Frozen == nil || (record.State != StateReady && record.State != StateReleased) {
		return codingrunner.FrozenSubmission{}, ErrState
	}
	artifact := record.Frozen.Artifact
	file, err := store.openObject(artifact.FrozenPatchSHA256, artifact.SizeBytes)
	if err != nil {
		return codingrunner.FrozenSubmission{}, err
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, artifact.SizeBytes+1))
	if err != nil || int64(len(body)) != artifact.SizeBytes {
		return codingrunner.FrozenSubmission{}, fmt.Errorf("%w: frozen object cannot be read", ErrCorrupt)
	}
	var patch frozenPatchDocument
	if err := json.Unmarshal(body, &patch); err != nil || patch.Schema != "dittobench-coding-frozen-patch-v1" {
		return codingrunner.FrozenSubmission{}, fmt.Errorf("%w: frozen patch envelope is invalid", ErrCorrupt)
	}
	metadata := record.Frozen.Metadata
	changedPaths := pathsFromChanges(patch.Changes)
	submission := codingrunner.FrozenSubmission{
		CodingContractVersion: patch.CodingContractVersion, CaseID: patch.CaseID,
		BaseTreeSHA256: patch.BaseTreeSHA256, VisibleBundleSHA256: patch.VisibleBundleSHA256,
		FinalTreeSHA256: metadata.FinalTreeSHA256, FrozenPatchSHA256: metadata.FrozenPatchSHA256,
		ChangedPathRoot: metadata.ChangedPathRoot, AuthoringEventRoot: metadata.AuthoringEventRoot,
		AuthoringTranscriptSHA256: metadata.AuthoringTranscriptSHA256,
		AuthoringTranscriptBytes:  metadata.AuthoringTranscriptBytes,
		ChangedPaths:              changedPaths, Changes: cloneChanges(patch.Changes),
		Patch: cloneBytes(body), ProtectedPathsIntact: metadata.ProtectedPathsIntact,
	}
	if err := codingrunner.ValidateFrozenSubmission(submission, record.Limits.runner()); err != nil ||
		!equalMetadata(metadataFromSubmission(submission), metadata) {
		return codingrunner.FrozenSubmission{}, fmt.Errorf("%w: frozen submission validation failed", ErrCorrupt)
	}
	return submission, nil
}

func validateSubmissionForRecord(submission codingrunner.FrozenSubmission, record *Record) error {
	if record.Transcript == nil || submission.CaseID != record.Binding.CaseID ||
		submission.AuthoringTranscriptSHA256 != record.Transcript.SHA256 ||
		submission.AuthoringTranscriptBytes != record.Transcript.SizeBytes {
		return ErrConflict
	}
	if err := codingrunner.ValidateFrozenSubmission(submission, record.Limits.runner()); err != nil {
		return fmt.Errorf("%w: frozen submission is invalid", ErrInvalid)
	}
	return nil
}

func metadataFromSubmission(submission codingrunner.FrozenSubmission) FrozenMetadata {
	return FrozenMetadata{
		CodingContractVersion: submission.CodingContractVersion, CaseID: submission.CaseID,
		BaseTreeSHA256: submission.BaseTreeSHA256, VisibleBundleSHA256: submission.VisibleBundleSHA256,
		FinalTreeSHA256: submission.FinalTreeSHA256, FrozenPatchSHA256: submission.FrozenPatchSHA256,
		ChangedPathRoot: submission.ChangedPathRoot, AuthoringEventRoot: submission.AuthoringEventRoot,
		AuthoringTranscriptSHA256: submission.AuthoringTranscriptSHA256,
		AuthoringTranscriptBytes:  submission.AuthoringTranscriptBytes,
		ProtectedPathsIntact:      submission.ProtectedPathsIntact,
	}
}

func equalMetadata(left, right FrozenMetadata) bool {
	return left == right
}

func pathsFromChanges(changes []codingrunner.FrozenChange) []string {
	if changes == nil {
		return nil
	}
	paths := make([]string, len(changes))
	for index, change := range changes {
		paths[index] = change.Path
	}
	return paths
}

func validFailure(failure codingrunner.FreezeFailure) bool {
	kindValid := failure.Kind == string(codingcontract.DomainRepairFailure) ||
		failure.Kind == string(codingcontract.DomainCandidateIntegrity) ||
		failure.Kind == string(codingcontract.DomainValidatorInfrastructure)
	return kindValid && validIdentifier(failure.Code, 128) &&
		lowerSHA256(failure.BaseTreeSHA256) && lowerSHA256(failure.VisibleBundleSHA256) &&
		lowerSHA256(failure.FinalTreeSHA256) && lowerSHA256(failure.ChangedPathRoot) &&
		lowerSHA256(failure.AuthoringEventRoot) && lowerSHA256(failure.AuthoringTranscriptSHA256) &&
		failure.AuthoringTranscriptBytes >= 0
}

func cloneChanges(values []codingrunner.FrozenChange) []codingrunner.FrozenChange {
	if values == nil {
		return nil
	}
	result := make([]codingrunner.FrozenChange, len(values))
	for index, value := range values {
		result[index] = value
		result[index].AfterContent = cloneBytes(value.AfterContent)
		if value.BeforeSHA256 != nil {
			copy := *value.BeforeSHA256
			result[index].BeforeSHA256 = &copy
		}
		if value.AfterSHA256 != nil {
			copy := *value.AfterSHA256
			result[index].AfterSHA256 = &copy
		}
	}
	return result
}

func cloneBytes(values []byte) []byte {
	if values == nil {
		return nil
	}
	result := make([]byte, len(values))
	copy(result, values)
	return result
}
