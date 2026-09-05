package codingrunner

import (
	"context"
	"errors"
	"io"
	"time"

	"github.com/google/uuid"
)

// HostedAuthority is supplied only by the trusted Platform after durable start
// and private grant verification. These values are not candidate-selected.
type HostedAuthority struct {
	EvaluationID     string
	AttemptID        string
	AssignmentSHA256 string
}

func (authority HostedAuthority) validate() error {
	evaluation, err := uuid.Parse(authority.EvaluationID)
	if err != nil || evaluation == uuid.Nil || evaluation.String() != authority.EvaluationID {
		return errors.New("hosted workspace authority is invalid")
	}
	attempt, err := uuid.Parse(authority.AttemptID)
	if err != nil || attempt == uuid.Nil || attempt.String() != authority.AttemptID || !isLowerSHA256(authority.AssignmentSHA256) {
		return errors.New("hosted workspace authority is invalid")
	}
	return nil
}

// NewHostedSession creates a native v2 workspace, not a v1 session relabeled
// after execution. The reused engine enforces the same filesystem, command,
// resource, transcript and freeze boundaries. No daemon or endpoint is enabled.
// The opaque evaluation/attempt replace v1 ticket/case identifiers internally;
// no v1 ticket lease is created or consulted.
func NewHostedSession(ctx context.Context, authority HostedAuthority, manifest Manifest, visibleBundle io.Reader, executor CommandExecutor) (*Session, error) {
	if err := ValidateHostedManifest(authority, manifest, time.Now()); err != nil {
		return nil, err
	}
	return newSession(ctx, manifest, visibleBundle, executor, HostedContractVersion, authority.AssignmentSHA256)
}

// ValidateHostedManifest preserves Manifest.Validate's permanently v1 behavior.
func ValidateHostedManifest(authority HostedAuthority, manifest Manifest, now time.Time) error {
	if err := authority.validate(); err != nil {
		return err
	}
	if manifest.TicketID != authority.EvaluationID || manifest.CaseID != authority.AttemptID {
		return errors.New("hosted workspace manifest does not match its authority")
	}
	if manifest.Deadline.After(now.Add(time.Hour)) {
		return errors.New("hosted workspace deadline exceeds one hour")
	}
	return manifest.validateVersion(now, HostedContractVersion)
}

// ReplayHostedFrozenSubmission validates the native v2 patch against the
// independently trusted assignment and reconstructs a pristine workspace.
// The v1 replay/validation entry points continue to reject these submissions.
func ReplayHostedFrozenSubmission(ctx context.Context, authority HostedAuthority, submission FrozenSubmission, visibleBundle io.Reader, limits Limits) (*ReplayWorkspace, error) {
	if err := ValidateHostedFrozenSubmission(authority, submission, limits); err != nil {
		return nil, err
	}
	return replayFrozenSubmission(ctx, submission, visibleBundle, limits, HostedContractVersion, authority.AssignmentSHA256, authority.EvaluationID)
}

func ValidateHostedFrozenSubmission(authority HostedAuthority, submission FrozenSubmission, limits Limits) error {
	if err := authority.validate(); err != nil {
		return err
	}
	if submission.CaseID != authority.AttemptID {
		return errors.New("hosted frozen submission does not match its authority")
	}
	if err := limits.Validate(); err != nil {
		return err
	}
	return validateFrozenSubmissionVersion(submission, limits, HostedContractVersion, authority.AssignmentSHA256, authority.EvaluationID)
}

func hostedEvaluationID(manifest Manifest) string {
	if manifest.CodingContractVersion == HostedContractVersion {
		return manifest.TicketID
	}
	return ""
}

func frozenPatchSchema(version int) string {
	if version == HostedContractVersion {
		return "dittobench-coding-frozen-patch-v2"
	}
	return "dittobench-coding-frozen-patch-v1"
}
