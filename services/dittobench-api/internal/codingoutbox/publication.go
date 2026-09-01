package codingoutbox

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingevidence"
	"github.com/google/uuid"
	"golang.org/x/sys/unix"
)

type authoringFreezeRequest struct {
	ValidatorHotkey              string          `json:"validator_hotkey"`
	AgentID                      string          `json:"agent_id"`
	BenchVersion                 int             `json:"bench_version"`
	RunRowID                     string          `json:"run_row_id"`
	TicketID                     string          `json:"ticket_id"`
	TicketDeadline               time.Time       `json:"ticket_deadline"`
	CodingRunID                  string          `json:"coding_run_id"`
	AgentArtifactSHA256          string          `json:"agent_artifact_sha256"`
	ScreenedImageSHA256          string          `json:"screened_image_sha256"`
	RunManifestSHA256            string          `json:"run_manifest_sha256"`
	TaskSetManifestSHA256        string          `json:"task_set_manifest_sha256"`
	AuthoringEvidenceSHA256      string          `json:"authoring_evidence_sha256"`
	AuthoringTranscriptObjectKey string          `json:"authoring_transcript_object_key"`
	AuthoringTranscriptBytes     int64           `json:"authoring_transcript_bytes"`
	AuthoringEventCount          uint64          `json:"authoring_event_count"`
	FrozenSubmissionObjectKey    string          `json:"frozen_submission_object_key"`
	Signature                    string          `json:"signature"`
	Evidence                     json.RawMessage `json:"evidence"`
}

// AuthoringPublicationOutcome is the complete non-secret phase result that a
// restarted supervisor must prove against the exact stored signed request
// before pristine grading can be restored.
type AuthoringPublicationOutcome struct {
	Evidence                     json.RawMessage
	AuthoringTranscriptObjectKey string
	AuthoringTranscriptBytes     int64
	AuthoringEventCount          uint64
	FrozenSubmissionObjectKey    string
}

type terminalResultRequest struct {
	ValidatorHotkey     string    `json:"validator_hotkey"`
	BenchVersion        int       `json:"bench_version"`
	RunRowID            string    `json:"run_row_id"`
	TicketID            string    `json:"ticket_id"`
	TicketDeadline      time.Time `json:"ticket_deadline"`
	AgentArtifactSHA256 string    `json:"agent_artifact_sha256"`
	ScreenedImageSHA256 string    `json:"screened_image_sha256"`
	RunEvidenceSHA256   string    `json:"run_evidence_sha256"`
	Signature           string    `json:"signature"`
	Evidence            struct {
		CodingRunID           string `json:"coding_run_id"`
		ValidatorTicketID     string `json:"validator_ticket_id"`
		RunManifestSHA256     string `json:"run_manifest_sha256"`
		TaskSetManifestSHA256 string `json:"task_set_manifest_sha256"`
	} `json:"evidence"`
}

type authoringFreezeAcknowledgement struct {
	FreezeID                string    `json:"freeze_id"`
	AgentID                 string    `json:"agent_id"`
	RunRowID                string    `json:"run_row_id"`
	TicketID                string    `json:"ticket_id"`
	CodingRunID             string    `json:"coding_run_id"`
	AuthoringEvidenceSHA256 string    `json:"authoring_evidence_sha256"`
	FrozenAt                time.Time `json:"frozen_at"`
	Accepted                *bool     `json:"accepted"`
	Idempotent              *bool     `json:"idempotent"`
	WeightEligible          *bool     `json:"weight_eligible"`
}

type terminalResultAcknowledgement struct {
	AgentID        string `json:"agent_id"`
	RunRowID       string `json:"run_row_id"`
	TicketID       string `json:"ticket_id"`
	CodingRunID    string `json:"coding_run_id"`
	Accepted       *bool  `json:"accepted"`
	Idempotent     *bool  `json:"idempotent"`
	WeightEligible *bool  `json:"weight_eligible"`
}

// PrepareAuthoringPublication stores the exact signed authoring-freeze request
// after the frozen patch and model evidence have been finalized locally.
func (attempt *Attempt) PrepareAuthoringPublication(
	ctx context.Context,
	authority PublicationAuthority,
	body []byte,
) (PublicationArtifact, error) {
	return attempt.preparePublication(ctx, PublicationAuthoringFreeze, authority, body)
}

// PrepareTerminalPublication stores the exact signed terminal-result request.
// A gradeable patch requires a durable authoring-freeze acknowledgement first;
// a terminal-without-patch record publishes directly.
func (attempt *Attempt) PrepareTerminalPublication(
	ctx context.Context,
	authority PublicationAuthority,
	body []byte,
) (PublicationArtifact, error) {
	return attempt.preparePublication(ctx, PublicationTerminalResult, authority, body)
}

func (attempt *Attempt) preparePublication(
	ctx context.Context,
	stage PublicationStage,
	authority PublicationAuthority,
	body []byte,
) (PublicationArtifact, error) {
	if attempt == nil || attempt.store == nil || ctx == nil || ctx.Err() != nil {
		return PublicationArtifact{}, ErrInvalid
	}
	if len(body) == 0 || len(body) > maximumPublicationRequestBytes {
		return PublicationArtifact{}, ErrInvalid
	}
	ownedBody := append([]byte(nil), body...)
	store := attempt.store
	now := store.config.Now().UTC()
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(now); err != nil {
		return PublicationArtifact{}, err
	}
	record, err := store.recordForAttempt(attempt.id)
	if err != nil {
		return PublicationArtifact{}, err
	}
	if record.Binding.Purpose != PurposeShadowAttempt || validatePublicationAuthority(authority) != nil {
		return PublicationArtifact{}, ErrInvalid
	}
	if authority.RunManifestSHA256 != record.Binding.AuthoritySHA256 {
		return PublicationArtifact{}, ErrConflict
	}
	if err := validatePublicationRequest(stage, record, authority, ownedBody); err != nil {
		return PublicationArtifact{}, err
	}
	existing := publicationForStage(record, stage)
	artifact := publicationArtifact(ownedBody)
	if existing != nil {
		if existing.Authority != authority || existing.Request != artifact {
			return PublicationArtifact{}, ErrConflict
		}
		if err := store.verifyPublicationArtifact(existing.Request, maximumPublicationRequestBytes); err != nil {
			return PublicationArtifact{}, err
		}
		return existing.Request, nil
	}
	if !record.Binding.Deadline.After(now) {
		return PublicationArtifact{}, ErrState
	}
	if !store.capacityHealthy() {
		return PublicationArtifact{}, ErrCapacity
	}
	switch stage {
	case PublicationAuthoringFreeze:
		if record.State != StateReady || record.Frozen == nil || record.AuthoringPublication != nil ||
			record.TerminalPublication != nil {
			return PublicationArtifact{}, ErrState
		}
	case PublicationTerminalResult:
		if record.TerminalPublication != nil ||
			(record.State != StateReady && record.State != StateTerminalWithoutPatch) ||
			(record.State == StateReady && (record.AuthoringPublication == nil ||
				record.AuthoringPublication.Acknowledgement == nil)) ||
			(record.State == StateTerminalWithoutPatch && record.AuthoringPublication != nil) {
			return PublicationArtifact{}, ErrState
		}
		if record.AuthoringPublication != nil &&
			!samePublicationRunAuthority(record.AuthoringPublication.Authority, authority) {
			return PublicationArtifact{}, ErrConflict
		}
	default:
		return PublicationArtifact{}, ErrInvalid
	}
	stored, err := store.storePublicationObject("publication-", ownedBody, maximumPublicationRequestBytes)
	if err != nil {
		return PublicationArtifact{}, err
	}
	updated := cloneRecord(record)
	publication := &PublicationRecord{
		Stage: stage, Authority: authority, Request: stored, PreparedAtUnix: now.Unix(),
	}
	if stage == PublicationAuthoringFreeze {
		updated.AuthoringPublication = publication
	} else {
		updated.TerminalPublication = publication
	}
	updated.Generation++
	if err := store.persistRecord(updated); err != nil {
		store.physicalKnown = false
		return PublicationArtifact{}, err
	}
	store.records[attempt.id] = updated
	return stored, nil
}

// AcknowledgeAuthoringPublication stores the exact verified Platform response
// and its freeze ID. It is a midpoint and never releases evidence retention.
func (attempt *Attempt) AcknowledgeAuthoringPublication(
	ctx context.Context,
	requestSHA256 string,
	body []byte,
) (PublicationArtifact, error) {
	return attempt.acknowledgePublication(ctx, PublicationAuthoringFreeze, requestSHA256, body)
}

// AcknowledgeTerminalPublication stores the exact verified terminal response.
// Shadow evidence may be released only after this transition commits.
func (attempt *Attempt) AcknowledgeTerminalPublication(
	ctx context.Context,
	requestSHA256 string,
	body []byte,
) (PublicationArtifact, error) {
	return attempt.acknowledgePublication(ctx, PublicationTerminalResult, requestSHA256, body)
}

func (attempt *Attempt) acknowledgePublication(
	ctx context.Context,
	stage PublicationStage,
	requestSHA256 string,
	body []byte,
) (PublicationArtifact, error) {
	if attempt == nil || attempt.store == nil || ctx == nil || ctx.Err() != nil {
		return PublicationArtifact{}, ErrInvalid
	}
	if len(body) == 0 || len(body) > maximumPublicationAckBytes {
		return PublicationArtifact{}, ErrInvalid
	}
	ownedBody := append([]byte(nil), body...)
	store := attempt.store
	now := store.config.Now().UTC()
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(now); err != nil {
		return PublicationArtifact{}, err
	}
	record, err := store.recordForAttempt(attempt.id)
	if err != nil {
		return PublicationArtifact{}, err
	}
	publication := publicationForStage(record, stage)
	if publication == nil {
		return PublicationArtifact{}, ErrState
	}
	if !lowerSHA256(requestSHA256) || requestSHA256 != publication.Request.SHA256 {
		return PublicationArtifact{}, ErrConflict
	}
	remoteID, err := validatePublicationAcknowledgement(stage, record, *publication, ownedBody)
	if err != nil {
		return PublicationArtifact{}, err
	}
	artifact := publicationArtifact(ownedBody)
	if publication.Acknowledgement != nil {
		if *publication.Acknowledgement != artifact || publication.RemoteAuthorityID != remoteID ||
			publication.AcknowledgedRequestSHA256 != requestSHA256 {
			return PublicationArtifact{}, ErrConflict
		}
		if err := store.verifyPublicationArtifact(artifact, maximumPublicationAckBytes); err != nil {
			return PublicationArtifact{}, err
		}
		return artifact, nil
	}
	if !store.capacityHealthy() {
		return PublicationArtifact{}, ErrCapacity
	}
	stored, err := store.storePublicationObject("acknowledgement-", ownedBody, maximumPublicationAckBytes)
	if err != nil {
		return PublicationArtifact{}, err
	}
	updated := cloneRecord(record)
	target := publicationForStage(updated, stage)
	target.Acknowledgement = &stored
	target.AcknowledgedRequestSHA256 = requestSHA256
	target.RemoteAuthorityID = remoteID
	target.AcknowledgedAtUnix = now.Unix()
	updated.Generation++
	if err := store.persistRecord(updated); err != nil {
		store.physicalKnown = false
		return PublicationArtifact{}, err
	}
	store.records[attempt.id] = updated
	return stored, nil
}

// PendingPublications returns only the next replayable signed request for each
// attempt. It deep-clones metadata and never returns request bytes inline.
func (store *Store) PendingPublications(ctx context.Context, limit int) ([]PendingPublication, error) {
	if ctx == nil || ctx.Err() != nil || limit <= 0 || limit > 10_000 {
		return nil, ErrInvalid
	}
	now := store.config.Now().UTC()
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(now); err != nil {
		return nil, err
	}
	values := make([]PendingPublication, 0)
	for _, record := range store.records {
		publication := pendingPublication(record)
		if publication == nil {
			continue
		}
		values = append(values, PendingPublication{
			RecordID: record.ID, Binding: record.Binding, Stage: publication.Stage,
			Authority: publication.Authority, Request: publication.Request,
		})
	}
	sort.Slice(values, func(left, right int) bool {
		leftRecord := store.records[values[left].RecordID]
		rightRecord := store.records[values[right].RecordID]
		leftPublication := publicationForStage(leftRecord, values[left].Stage)
		rightPublication := publicationForStage(rightRecord, values[right].Stage)
		if leftPublication.PreparedAtUnix == rightPublication.PreparedAtUnix {
			return values[left].RecordID < values[right].RecordID
		}
		return leftPublication.PreparedAtUnix < rightPublication.PreparedAtUnix
	})
	if len(values) > limit {
		values = values[:limit]
	}
	return values, nil
}

func (store *Store) OpenPublication(
	ctx context.Context,
	id string,
	stage PublicationStage,
) (io.ReadCloser, error) {
	return store.openPublicationPart(ctx, id, stage, false)
}

func (store *Store) OpenPublicationAcknowledgement(
	ctx context.Context,
	id string,
	stage PublicationStage,
) (io.ReadCloser, error) {
	return store.openPublicationPart(ctx, id, stage, true)
}

// ValidateAuthoringPublicationOutcome binds restart grading to the exact
// acknowledged signed authoring request, not merely to the existence of an
// outbox record for the same ticket.
func (attempt *Attempt) ValidateAuthoringPublicationOutcome(
	ctx context.Context,
	outcome AuthoringPublicationOutcome,
) error {
	if attempt == nil || attempt.store == nil || ctx == nil || ctx.Err() != nil ||
		len(outcome.Evidence) == 0 || outcome.AuthoringTranscriptObjectKey == "" ||
		outcome.AuthoringTranscriptBytes <= 0 || outcome.AuthoringEventCount == 0 ||
		outcome.FrozenSubmissionObjectKey == "" {
		return ErrInvalid
	}
	acknowledgement, err := attempt.store.OpenPublicationAcknowledgement(
		ctx, attempt.id, PublicationAuthoringFreeze,
	)
	if err != nil {
		return err
	}
	if err := acknowledgement.Close(); err != nil {
		return err
	}
	reader, err := attempt.store.OpenPublication(ctx, attempt.id, PublicationAuthoringFreeze)
	if err != nil {
		return err
	}
	body, readErr := io.ReadAll(io.LimitReader(reader, maximumPublicationRequestBytes+1))
	closeErr := reader.Close()
	if readErr != nil || closeErr != nil || len(body) == 0 || int64(len(body)) > maximumPublicationRequestBytes {
		return errors.Join(ErrCorrupt, readErr, closeErr)
	}
	var request authoringFreezeRequest
	if err := decodeRequiredPublication(body, &request,
		"validator_hotkey", "agent_id", "bench_version", "run_row_id", "ticket_id", "ticket_deadline",
		"coding_run_id", "agent_artifact_sha256", "screened_image_sha256", "run_manifest_sha256",
		"task_set_manifest_sha256", "authoring_evidence_sha256", "evidence",
		"authoring_transcript_object_key", "authoring_transcript_bytes", "authoring_event_count",
		"frozen_submission_object_key", "signature"); err != nil {
		return ErrCorrupt
	}
	if !canonicalJSONEqual(request.Evidence, outcome.Evidence) ||
		request.AuthoringTranscriptObjectKey != outcome.AuthoringTranscriptObjectKey ||
		request.AuthoringTranscriptBytes != outcome.AuthoringTranscriptBytes ||
		request.AuthoringEventCount != outcome.AuthoringEventCount ||
		request.FrozenSubmissionObjectKey != outcome.FrozenSubmissionObjectKey {
		return ErrConflict
	}
	return nil
}

func canonicalJSONEqual(left, right []byte) bool {
	canonical := func(body []byte) ([]byte, error) {
		if codingcontract.ValidateJSONDocument(body, maximumPublicationRequestBytes) != nil {
			return nil, ErrInvalid
		}
		decoder := json.NewDecoder(bytes.NewReader(body))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil {
			return nil, err
		}
		var trailing any
		if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
			return nil, ErrInvalid
		}
		return json.Marshal(value)
	}
	leftCanonical, leftErr := canonical(left)
	rightCanonical, rightErr := canonical(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftCanonical, rightCanonical)
}

func (store *Store) openPublicationPart(
	ctx context.Context,
	id string,
	stage PublicationStage,
	acknowledgement bool,
) (io.ReadCloser, error) {
	if ctx == nil || ctx.Err() != nil || !lowerSHA256(id) || !validPublicationStage(stage) {
		return nil, ErrInvalid
	}
	now := store.config.Now().UTC()
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(now); err != nil {
		return nil, err
	}
	record := store.records[id]
	publication := publicationForStage(record, stage)
	if publication == nil {
		return nil, ErrState
	}
	artifact := publication.Request
	if acknowledgement {
		if publication.Acknowledgement == nil {
			return nil, ErrState
		}
		artifact = *publication.Acknowledgement
	}
	return store.openObject(artifact.SHA256, artifact.SizeBytes)
}

func (store *Store) storePublicationObject(prefix string, body []byte, maximum int64) (PublicationArtifact, error) {
	if len(body) == 0 || int64(len(body)) > maximum {
		return PublicationArtifact{}, ErrInvalid
	}
	artifact := publicationArtifact(body)
	file, name, dev, ino, err := newStagingFile(store.dirs.staging, prefix)
	if err != nil {
		return PublicationArtifact{}, err
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
	if err := writeAll(file, body); err != nil || file.Sync() != nil || file.Chmod(0o400) != nil ||
		file.Sync() != nil || file.Close() != nil {
		return PublicationArtifact{}, errors.New("persist coding publication bytes")
	}
	if err := store.installObject(name, dev, ino, artifact.SHA256, artifact.SizeBytes); err != nil {
		return PublicationArtifact{}, err
	}
	keep = true
	return artifact, nil
}

func validatePublicationRequest(
	stage PublicationStage,
	record *Record,
	authority PublicationAuthority,
	body []byte,
) error {
	if err := validatePublicationDocument(body, maximumPublicationRequestBytes); err != nil {
		return err
	}
	switch stage {
	case PublicationAuthoringFreeze:
		if record.Frozen == nil || record.Transcript == nil {
			return ErrState
		}
		var request authoringFreezeRequest
		if err := decodeRequiredPublication(body, &request,
			"validator_hotkey", "agent_id", "bench_version", "run_row_id", "ticket_id", "ticket_deadline",
			"coding_run_id", "agent_artifact_sha256", "screened_image_sha256", "run_manifest_sha256",
			"task_set_manifest_sha256", "authoring_evidence_sha256", "evidence",
			"authoring_transcript_object_key", "authoring_transcript_bytes", "authoring_event_count",
			"frozen_submission_object_key", "signature"); err != nil {
			return err
		}
		var evidence codingcontract.AuthoringEvidence
		if err := json.Unmarshal(request.Evidence, &evidence); err != nil || evidence.Validate() != nil {
			return ErrInvalid
		}
		if !validValidatorHotkey(request.ValidatorHotkey) || !validSignature(request.Signature) ||
			request.AgentID != authority.AgentID || request.BenchVersion != authority.BenchVersion ||
			request.RunRowID != authority.RunRowID ||
			request.TicketID != record.Binding.TicketID || !request.TicketDeadline.Equal(record.Binding.Deadline) ||
			request.CodingRunID != authority.CodingRunID || request.AgentArtifactSHA256 != record.Binding.AgentArtifactSHA256 ||
			request.ScreenedImageSHA256 != authority.ScreenedImageSHA256 ||
			request.RunManifestSHA256 != authority.RunManifestSHA256 ||
			request.TaskSetManifestSHA256 != authority.TaskSetManifestSHA256 ||
			request.AuthoringEvidenceSHA256 != authority.EvidenceSHA256 ||
			request.AuthoringTranscriptObjectKey != record.Transcript.ObjectKey ||
			request.AuthoringTranscriptBytes != record.Transcript.SizeBytes ||
			request.AuthoringEventCount != record.Transcript.Events ||
			request.FrozenSubmissionObjectKey != record.Frozen.Artifact.ObjectKey ||
			evidence.AuthoringTranscriptSHA256 != record.Transcript.SHA256 ||
			evidence.FrozenPatchSHA256 != record.Frozen.Artifact.FrozenPatchSHA256 {
			return ErrConflict
		}
	case PublicationTerminalResult:
		var request terminalResultRequest
		if err := decodeRequiredPublication(body, &request,
			"validator_hotkey", "bench_version", "run_row_id", "ticket_id", "ticket_deadline",
			"agent_artifact_sha256", "screened_image_sha256", "run_evidence_sha256", "evidence", "signature"); err != nil {
			return err
		}
		if !validValidatorHotkey(request.ValidatorHotkey) || !validSignature(request.Signature) ||
			request.BenchVersion != authority.BenchVersion || request.RunRowID != authority.RunRowID ||
			request.TicketID != record.Binding.TicketID ||
			!request.TicketDeadline.Equal(record.Binding.Deadline) ||
			request.AgentArtifactSHA256 != record.Binding.AgentArtifactSHA256 ||
			request.ScreenedImageSHA256 != authority.ScreenedImageSHA256 ||
			request.RunEvidenceSHA256 != authority.EvidenceSHA256 ||
			request.Evidence.CodingRunID != authority.CodingRunID ||
			request.Evidence.ValidatorTicketID != record.Binding.TicketID ||
			request.Evidence.RunManifestSHA256 != authority.RunManifestSHA256 ||
			request.Evidence.TaskSetManifestSHA256 != authority.TaskSetManifestSHA256 {
			return ErrConflict
		}
	default:
		return ErrInvalid
	}
	return nil
}

func validatePublicationAcknowledgement(
	stage PublicationStage,
	record *Record,
	publication PublicationRecord,
	body []byte,
) (string, error) {
	if err := validatePublicationDocument(body, maximumPublicationAckBytes); err != nil {
		return "", err
	}
	authority := publication.Authority
	switch stage {
	case PublicationAuthoringFreeze:
		var acknowledgement authoringFreezeAcknowledgement
		if err := decodeRequiredPublication(body, &acknowledgement,
			"freeze_id", "agent_id", "run_row_id", "ticket_id", "coding_run_id",
			"authoring_evidence_sha256", "frozen_at", "accepted", "idempotent", "weight_eligible"); err != nil {
			return "", err
		}
		if !canonicalUUID(acknowledgement.FreezeID) || acknowledgement.AgentID != authority.AgentID ||
			acknowledgement.RunRowID != authority.RunRowID || acknowledgement.TicketID != record.Binding.TicketID ||
			acknowledgement.CodingRunID != authority.CodingRunID ||
			acknowledgement.AuthoringEvidenceSHA256 != authority.EvidenceSHA256 ||
			acknowledgement.FrozenAt.IsZero() || acknowledgement.FrozenAt.Unix() < publication.PreparedAtUnix ||
			acknowledgement.FrozenAt.After(record.Binding.Deadline) ||
			!acceptedShadowAcknowledgement(acknowledgement.Accepted, acknowledgement.Idempotent,
				acknowledgement.WeightEligible) {
			return "", ErrConflict
		}
		return acknowledgement.FreezeID, nil
	case PublicationTerminalResult:
		var acknowledgement terminalResultAcknowledgement
		if err := decodeRequiredPublication(body, &acknowledgement,
			"agent_id", "run_row_id", "ticket_id", "coding_run_id",
			"accepted", "idempotent", "weight_eligible"); err != nil {
			return "", err
		}
		if acknowledgement.AgentID != authority.AgentID || acknowledgement.RunRowID != authority.RunRowID ||
			acknowledgement.TicketID != record.Binding.TicketID ||
			acknowledgement.CodingRunID != authority.CodingRunID ||
			!acceptedShadowAcknowledgement(acknowledgement.Accepted, acknowledgement.Idempotent,
				acknowledgement.WeightEligible) {
			return "", ErrConflict
		}
		return acknowledgement.CodingRunID, nil
	default:
		return "", ErrInvalid
	}
}

func decodeRequiredPublication(body []byte, target any, fields ...string) error {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var shape map[string]json.RawMessage
	if err := decoder.Decode(&shape); err != nil {
		return ErrInvalid
	}
	for _, field := range fields {
		if _, ok := shape[field]; !ok {
			return ErrInvalid
		}
	}
	if err := json.Unmarshal(body, target); err != nil {
		return ErrInvalid
	}
	return nil
}

func validatePublicationDocument(body []byte, maximum int64) error {
	if len(body) == 0 || int64(len(body)) > maximum ||
		codingcontract.ValidateJSONDocument(body, int(maximum)) != nil {
		return ErrInvalid
	}
	return nil
}

func validatePublicationAuthority(authority PublicationAuthority) error {
	if !canonicalUUID(authority.AgentID) || !canonicalUUID(authority.RunRowID) ||
		authority.BenchVersion < 7 || authority.BenchVersion > 1_000_000 ||
		!validIdentifier(authority.CodingRunID, 256) ||
		!lowerSHA256(authority.ScreenedImageSHA256) || !lowerSHA256(authority.RunManifestSHA256) ||
		!lowerSHA256(authority.TaskSetManifestSHA256) || !lowerSHA256(authority.EvidenceSHA256) {
		return ErrInvalid
	}
	return nil
}

func acceptedShadowAcknowledgement(accepted, idempotent, weightEligible *bool) bool {
	return accepted != nil && *accepted && idempotent != nil && weightEligible != nil && !*weightEligible
}

func publicationArtifact(body []byte) PublicationArtifact {
	digest := sha256.Sum256(body)
	sha := hex.EncodeToString(digest[:])
	return PublicationArtifact{ObjectKey: "sha256/" + sha, SHA256: sha, SizeBytes: int64(len(body))}
}

func publicationForStage(record *Record, stage PublicationStage) *PublicationRecord {
	if record == nil {
		return nil
	}
	if stage == PublicationAuthoringFreeze {
		return record.AuthoringPublication
	}
	if stage == PublicationTerminalResult {
		return record.TerminalPublication
	}
	return nil
}

func pendingPublication(record *Record) *PublicationRecord {
	if record == nil || (record.State != StateReady && record.State != StateTerminalWithoutPatch) {
		return nil
	}
	if record.AuthoringPublication != nil && record.AuthoringPublication.Acknowledgement == nil {
		return record.AuthoringPublication
	}
	if record.TerminalPublication != nil && record.TerminalPublication.Acknowledgement == nil {
		return record.TerminalPublication
	}
	return nil
}

func validPublicationStage(stage PublicationStage) bool {
	return stage == PublicationAuthoringFreeze || stage == PublicationTerminalResult
}

func (store *Store) verifyPublicationArtifact(artifact PublicationArtifact, maximum int64) error {
	if artifact.ObjectKey != "sha256/"+artifact.SHA256 || !lowerSHA256(artifact.SHA256) ||
		artifact.SizeBytes <= 0 || artifact.SizeBytes > maximum {
		return ErrCorrupt
	}
	return store.verifyReference(artifact.ObjectKey, artifact.SHA256, artifact.SizeBytes)
}

func validatePublicationRecords(record *Record) error {
	if record == nil {
		return fmt.Errorf("%w: publication record is unavailable", ErrCorrupt)
	}
	if record.State == StateReserved || record.State == StateCollecting || record.State == StateExpired {
		if record.AuthoringPublication != nil || record.TerminalPublication != nil {
			return fmt.Errorf("%w: inactive record carries publication state", ErrCorrupt)
		}
		return nil
	}
	if record.Binding.Purpose != PurposeShadowAttempt &&
		(record.AuthoringPublication != nil || record.TerminalPublication != nil) {
		return fmt.Errorf("%w: non-shadow record carries publication state", ErrCorrupt)
	}
	updatedAtUnix := time.Unix(0, record.UpdatedAtUnixNano).UTC().Unix()
	if record.AuthoringPublication != nil {
		publication := record.AuthoringPublication
		if publication.Stage != PublicationAuthoringFreeze || record.Frozen == nil || record.Failure != nil ||
			validatePublicationAuthority(publication.Authority) != nil ||
			publication.Authority.RunManifestSHA256 != record.Binding.AuthoritySHA256 ||
			publication.PreparedAtUnix < record.SealedAtUnix || publication.PreparedAtUnix > updatedAtUnix ||
			!time.Unix(publication.PreparedAtUnix, 0).UTC().Before(record.Binding.Deadline) ||
			publication.AcknowledgedAtUnix > updatedAtUnix ||
			validatePublicationArtifactShape(publication.Request, maximumPublicationRequestBytes) != nil ||
			!validAcknowledgementShape(publication, maximumPublicationAckBytes, true) {
			return fmt.Errorf("%w: authoring publication state disagrees", ErrCorrupt)
		}
	}
	if record.TerminalPublication != nil {
		publication := record.TerminalPublication
		if publication.Stage != PublicationTerminalResult ||
			validatePublicationAuthority(publication.Authority) != nil ||
			publication.Authority.RunManifestSHA256 != record.Binding.AuthoritySHA256 ||
			publication.PreparedAtUnix < record.SealedAtUnix || publication.PreparedAtUnix > updatedAtUnix ||
			!time.Unix(publication.PreparedAtUnix, 0).UTC().Before(record.Binding.Deadline) ||
			publication.AcknowledgedAtUnix > updatedAtUnix ||
			validatePublicationArtifactShape(publication.Request, maximumPublicationRequestBytes) != nil ||
			!validAcknowledgementShape(publication, maximumPublicationAckBytes, false) {
			return fmt.Errorf("%w: terminal publication state disagrees", ErrCorrupt)
		}
		if record.Frozen != nil && (record.AuthoringPublication == nil ||
			record.AuthoringPublication.Acknowledgement == nil) {
			return fmt.Errorf("%w: terminal publication lacks authoring acknowledgement", ErrCorrupt)
		}
		if record.Failure != nil && record.AuthoringPublication != nil {
			return fmt.Errorf("%w: terminal failure carries authoring publication", ErrCorrupt)
		}
	}
	if record.AuthoringPublication != nil && record.TerminalPublication != nil {
		left, right := record.AuthoringPublication.Authority, record.TerminalPublication.Authority
		if !samePublicationRunAuthority(left, right) {
			return fmt.Errorf("%w: publication stages disagree", ErrCorrupt)
		}
	}
	if record.State == StateReleased && record.Binding.Purpose == PurposeShadowAttempt {
		if record.TerminalPublication == nil || record.TerminalPublication.Acknowledgement == nil ||
			record.ReleaseFinalization == nil ||
			record.ReleaseEvidenceSHA256 != record.TerminalPublication.Authority.EvidenceSHA256 ||
			record.ReleaseFinalization.TicketID != record.Binding.TicketID ||
			record.ReleaseFinalization.EvidenceKind != codingevidence.KindTerminalPublicationAcknowledgement ||
			record.ReleaseFinalization.SHA256 != record.TerminalPublication.Acknowledgement.SHA256 ||
			record.ReleaseFinalization.SizeBytes != record.TerminalPublication.Acknowledgement.SizeBytes ||
			!canonicalUUID(record.ReleaseFinalization.UploadID) ||
			record.ReleaseFinalization.ClaimGeneration < 1 ||
			record.ReleaseFinalization.ClaimGeneration > (1<<31)-1 ||
			record.ReleaseFinalization.FinalizedAtUnixNano <= 0 ||
			time.Unix(0, record.ReleaseFinalization.FinalizedAtUnixNano).UTC().After(record.Binding.Deadline) ||
			record.ReleasedAtUnix < record.TerminalPublication.AcknowledgedAtUnix {
			return fmt.Errorf("%w: shadow release lacks terminal acknowledgement", ErrCorrupt)
		}
	} else if record.State == StateReleased && record.ReleaseFinalization != nil {
		return fmt.Errorf("%w: non-shadow release carries remote finalization", ErrCorrupt)
	}
	return nil
}

func validAcknowledgementShape(publication *PublicationRecord, maximum int64, requireRemoteUUID bool) bool {
	if publication.Acknowledgement == nil {
		return publication.AcknowledgedRequestSHA256 == "" && publication.RemoteAuthorityID == "" &&
			publication.AcknowledgedAtUnix == 0
	}
	if validatePublicationArtifactShape(*publication.Acknowledgement, maximum) != nil ||
		publication.AcknowledgedAtUnix < publication.PreparedAtUnix ||
		publication.AcknowledgedRequestSHA256 != publication.Request.SHA256 ||
		!validIdentifier(publication.RemoteAuthorityID, 256) {
		return false
	}
	return !requireRemoteUUID || canonicalUUID(publication.RemoteAuthorityID)
}

func samePublicationRunAuthority(left, right PublicationAuthority) bool {
	left.EvidenceSHA256 = ""
	right.EvidenceSHA256 = ""
	return left == right
}

func validatePublicationArtifactShape(artifact PublicationArtifact, maximum int64) error {
	if artifact.ObjectKey != "sha256/"+artifact.SHA256 || !lowerSHA256(artifact.SHA256) ||
		artifact.SizeBytes <= 0 || artifact.SizeBytes > maximum {
		return ErrCorrupt
	}
	return nil
}

func (store *Store) verifyPublicationReferences(record *Record) error {
	for _, publication := range []*PublicationRecord{record.AuthoringPublication, record.TerminalPublication} {
		if publication == nil {
			continue
		}
		request, err := store.readPublicationArtifact(publication.Request, maximumPublicationRequestBytes)
		if err != nil {
			return err
		}
		if err := validatePublicationRequest(publication.Stage, record, publication.Authority, request); err != nil {
			return fmt.Errorf("%w: stored publication request disagrees", ErrCorrupt)
		}
		if publication.Acknowledgement == nil {
			continue
		}
		acknowledgement, err := store.readPublicationArtifact(*publication.Acknowledgement, maximumPublicationAckBytes)
		if err != nil {
			return err
		}
		remoteID, err := validatePublicationAcknowledgement(publication.Stage, record, *publication, acknowledgement)
		if err != nil || remoteID != publication.RemoteAuthorityID {
			return fmt.Errorf("%w: stored publication acknowledgement disagrees", ErrCorrupt)
		}
	}
	return nil
}

func (store *Store) readPublicationArtifact(artifact PublicationArtifact, maximum int64) ([]byte, error) {
	if validatePublicationArtifactShape(artifact, maximum) != nil {
		return nil, ErrCorrupt
	}
	file, err := store.openObject(artifact.SHA256, artifact.SizeBytes)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, artifact.SizeBytes+1))
	if err != nil || int64(len(body)) != artifact.SizeBytes {
		return nil, ErrCorrupt
	}
	return body, nil
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func validValidatorHotkey(value string) bool {
	if len(value) != 47 && len(value) != 48 {
		return false
	}
	for _, character := range value {
		if !strings.ContainsRune("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", character) {
			return false
		}
	}
	return true
}

func validSignature(value string) bool {
	if len(value) != 128 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func clonePublication(value *PublicationRecord) *PublicationRecord {
	if value == nil {
		return nil
	}
	copy := *value
	if value.Acknowledgement != nil {
		acknowledgement := *value.Acknowledgement
		copy.Acknowledgement = &acknowledgement
	}
	return &copy
}

func addPublicationReferences(references map[string]struct{}, record *Record) {
	for _, publication := range []*PublicationRecord{record.AuthoringPublication, record.TerminalPublication} {
		if publication == nil {
			continue
		}
		references[publication.Request.SHA256] = struct{}{}
		if publication.Acknowledgement != nil {
			references[publication.Acknowledgement.SHA256] = struct{}{}
		}
	}
}
