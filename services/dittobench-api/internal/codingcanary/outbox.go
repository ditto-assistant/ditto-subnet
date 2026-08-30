package codingcanary

import (
	"context"
	"errors"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

type outboxTranscriptSink struct {
	attempt *codingoutbox.Attempt
}

type outboxFrozenSink struct {
	attempt *codingoutbox.Attempt
}

type transcriptAdapter struct {
	inner codingoutbox.TranscriptWriter
}

func (sink outboxTranscriptSink) Begin(
	ctx context.Context,
	binding codingcertifier.EvidenceBinding,
) (codingcertifier.TranscriptWriter, error) {
	stored, err := sink.attempt.Binding()
	if err != nil || stored.TicketID != binding.TicketID || stored.CaseID != binding.CaseID ||
		stored.ProfileCapabilityID != binding.ProfileCapabilityID ||
		stored.HarnessInstanceID != binding.HarnessInstanceID {
		return nil, errors.Join(ErrInvalid, err)
	}
	writer, err := sink.attempt.BeginTranscript(ctx)
	if err != nil {
		return nil, err
	}
	return transcriptAdapter{inner: writer}, nil
}

func (adapter transcriptAdapter) Write(body []byte) (int, error) {
	return adapter.inner.Write(body)
}

func (adapter transcriptAdapter) Commit(
	ctx context.Context,
	identity codingrunner.TranscriptIdentity,
) (codingcertifier.TranscriptArtifact, error) {
	artifact, err := adapter.inner.Commit(ctx, identity)
	if err != nil {
		return codingcertifier.TranscriptArtifact{}, err
	}
	return codingcertifier.TranscriptArtifact{
		ObjectKey: artifact.ObjectKey, SHA256: artifact.SHA256,
		SizeBytes: artifact.SizeBytes, Events: artifact.Events,
	}, nil
}

func (adapter transcriptAdapter) Abort() error {
	return adapter.inner.Abort()
}

func (sink outboxFrozenSink) Store(
	ctx context.Context,
	binding codingcertifier.EvidenceBinding,
	submission codingrunner.FrozenSubmission,
) (codingcertifier.FrozenSubmissionArtifact, error) {
	stored, err := sink.attempt.Binding()
	if err != nil || stored.TicketID != binding.TicketID || stored.CaseID != binding.CaseID ||
		stored.ProfileCapabilityID != binding.ProfileCapabilityID {
		return codingcertifier.FrozenSubmissionArtifact{}, errors.Join(ErrInvalid, err)
	}
	artifact, err := sink.attempt.StoreFrozen(ctx, submission)
	if err != nil {
		return codingcertifier.FrozenSubmissionArtifact{}, err
	}
	return codingcertifier.FrozenSubmissionArtifact{
		ObjectKey: artifact.ObjectKey, FrozenPatchSHA256: artifact.FrozenPatchSHA256,
		FinalTreeSHA256: artifact.FinalTreeSHA256, ChangedPathRoot: artifact.ChangedPathRoot,
	}, nil
}
