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

var sealedEvidenceKinds = [...]codingevidence.Kind{
	codingevidence.KindAuthoringTranscript,
	codingevidence.KindFrozenSubmission,
	codingevidence.KindAuthoringPublicationRequest,
	codingevidence.KindAuthoringPublicationAcknowledgement,
	codingevidence.KindTerminalPublicationRequest,
	codingevidence.KindTerminalPublicationAcknowledgement,
}

// SealedEvidenceManifest returns the canonical available identities for one
// outbox record. It contains no body, filesystem path, or remote storage key.
func (store *Store) SealedEvidenceManifest(
	ctx context.Context,
	id string,
) ([]SealedEvidenceArtifact, error) {
	if ctx == nil || ctx.Err() != nil || !lowerSHA256(id) {
		return nil, ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(store.config.Now().UTC()); err != nil {
		return nil, err
	}
	record := store.records[id]
	if record == nil || record.Binding.Purpose != PurposeShadowAttempt {
		return nil, ErrState
	}
	manifest := make([]SealedEvidenceArtifact, 0, len(sealedEvidenceKinds))
	for _, kind := range sealedEvidenceKinds {
		artifact, available, err := sealedEvidenceArtifact(record, kind)
		if err != nil {
			return nil, err
		}
		if available {
			manifest = append(manifest, artifact)
		}
	}
	return manifest, nil
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
	_, known := codingevidence.MaximumSize(kind)
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
	artifact, available, err := sealedEvidenceArtifact(record, kind)
	if err != nil {
		return zero, nil, err
	}
	if !available {
		return zero, nil, ErrState
	}
	reader, err := store.openObject(artifact.SHA256, artifact.SizeBytes)
	if err != nil {
		return zero, nil, err
	}
	return artifact, reader, nil
}

func sealedEvidenceArtifact(
	record *Record,
	kind codingevidence.Kind,
) (SealedEvidenceArtifact, bool, error) {
	var objectKey, digest string
	var size int64
	switch kind {
	case codingevidence.KindAuthoringTranscript:
		if record.Transcript == nil {
			return SealedEvidenceArtifact{}, false, nil
		}
		objectKey, digest, size = record.Transcript.ObjectKey, record.Transcript.SHA256, record.Transcript.SizeBytes
	case codingevidence.KindFrozenSubmission:
		if record.Frozen == nil {
			return SealedEvidenceArtifact{}, false, nil
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
			return SealedEvidenceArtifact{}, false, nil
		}
		artifact := publication.Request
		if kind == codingevidence.KindAuthoringPublicationAcknowledgement ||
			kind == codingevidence.KindTerminalPublicationAcknowledgement {
			if publication.Acknowledgement == nil {
				return SealedEvidenceArtifact{}, false, nil
			}
			artifact = *publication.Acknowledgement
		}
		objectKey, digest, size = artifact.ObjectKey, artifact.SHA256, artifact.SizeBytes
	default:
		return SealedEvidenceArtifact{}, false, ErrInvalid
	}
	maximum, known := codingevidence.MaximumSize(kind)
	if !known {
		return SealedEvidenceArtifact{}, false, ErrInvalid
	}
	if size < 1 || size > maximum || objectKey != "sha256/"+digest {
		return SealedEvidenceArtifact{}, false, ErrCorrupt
	}
	return SealedEvidenceArtifact{Kind: kind, SHA256: digest, SizeBytes: size}, true, nil
}
