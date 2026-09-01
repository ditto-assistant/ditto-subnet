package codingoutbox

import (
	"context"
	"io"

	"github.com/ditto-assistant/dittobench-api/internal/codingevidence"
)

// SealedEvidenceArtifact identifies one immutable outbox object without
// exposing a filesystem path or its body.
type SealedEvidenceArtifact struct {
	Kind      codingevidence.Kind
	SHA256    string
	SizeBytes int64
}

// OpenSealedEvidence resolves and opens one known evidence kind by exact record
// identity. Released records remain readable until their configured retention
// sweep removes them.
func (store *Store) OpenSealedEvidence(
	ctx context.Context,
	id string,
	kind codingevidence.Kind,
) (SealedEvidenceArtifact, io.ReadCloser, error) {
	var zero SealedEvidenceArtifact
	maximum, known := codingevidence.MaximumSize(kind)
	if ctx == nil || ctx.Err() != nil || !lowerSHA256(id) || !known {
		return zero, nil, ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(store.config.Now().UTC()); err != nil {
		return zero, nil, err
	}
	record := store.records[id]
	if record == nil {
		return zero, nil, ErrState
	}
	var objectKey, digest string
	var size int64
	switch kind {
	case codingevidence.KindAuthoringTranscript:
		if record.Transcript == nil {
			return zero, nil, ErrState
		}
		objectKey, digest, size = record.Transcript.ObjectKey, record.Transcript.SHA256, record.Transcript.SizeBytes
	case codingevidence.KindFrozenSubmission:
		if record.Frozen == nil {
			return zero, nil, ErrState
		}
		artifact := record.Frozen.Artifact
		objectKey, digest, size = artifact.ObjectKey, artifact.FrozenPatchSHA256, artifact.SizeBytes
	case codingevidence.KindAuthoringPublicationRequest,
		codingevidence.KindAuthoringPublicationAcknowledgement,
		codingevidence.KindTerminalPublicationRequest,
		codingevidence.KindTerminalPublicationAcknowledgement:
		publication := record.AuthoringPublication
		if kind == codingevidence.KindTerminalPublicationRequest ||
			kind == codingevidence.KindTerminalPublicationAcknowledgement {
			publication = record.TerminalPublication
		}
		if publication == nil {
			return zero, nil, ErrState
		}
		artifact := publication.Request
		if kind == codingevidence.KindAuthoringPublicationAcknowledgement ||
			kind == codingevidence.KindTerminalPublicationAcknowledgement {
			if publication.Acknowledgement == nil {
				return zero, nil, ErrState
			}
			artifact = *publication.Acknowledgement
		}
		objectKey, digest, size = artifact.ObjectKey, artifact.SHA256, artifact.SizeBytes
	default:
		return zero, nil, ErrInvalid
	}
	if size < 1 || size > maximum || objectKey != "sha256/"+digest {
		return zero, nil, ErrCorrupt
	}
	reader, err := store.openObject(digest, size)
	if err != nil {
		return zero, nil, err
	}
	return SealedEvidenceArtifact{Kind: kind, SHA256: digest, SizeBytes: size}, reader, nil
}
