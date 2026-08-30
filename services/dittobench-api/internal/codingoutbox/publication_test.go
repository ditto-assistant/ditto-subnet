package codingoutbox

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

type publicationFixture struct {
	store      *Store
	clock      *fixtureClock
	root       string
	binding    Binding
	attempt    *Attempt
	transcript TranscriptArtifact
	frozen     FrozenArtifact
	authority  PublicationAuthority
	limits     codingrunner.Limits
}

func newPublicationFixture(t *testing.T, suffix string) publicationFixture {
	t.Helper()
	store, clock, root := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, suffix)
	binding.Purpose = PurposeShadowAttempt
	binding.ExecutionID = "shadow-attempt-" + suffix
	binding.HarnessAuthoritySHA256 = strings.Repeat("e", 64)
	binding.ScreenedImageSHA256 = strings.Repeat("d", 64)
	limits := codingrunner.DefaultLimits()
	attempt, err := store.Reserve(t.Context(), binding, limits)
	if err != nil {
		t.Fatal(err)
	}
	transcript := commitFixtureTranscript(t, attempt, []byte("{\"sequence\":1}\n"))
	submission := fixtureSubmission(t, binding, transcript)
	frozen, err := attempt.StoreFrozen(t.Context(), submission)
	if err != nil {
		t.Fatal(err)
	}
	return publicationFixture{
		store: store, clock: clock, root: root, binding: binding, attempt: attempt,
		transcript: transcript, frozen: frozen, limits: limits,
		authority: PublicationAuthority{
			AgentID: "11111111-1111-4111-8111-111111111111", BenchVersion: 12,
			RunRowID:              "22222222-2222-4222-8222-222222222222",
			CodingRunID:           "coding-run-" + suffix,
			ScreenedImageSHA256:   strings.Repeat("d", 64),
			RunManifestSHA256:     binding.AuthoritySHA256,
			TaskSetManifestSHA256: strings.Repeat("f", 64),
			EvidenceSHA256:        strings.Repeat("c", 64),
		},
	}
}

func (fixture publicationFixture) authoringRequest(t *testing.T) []byte {
	t.Helper()
	body, err := json.Marshal(map[string]any{
		"validator_hotkey":          strings.Repeat("5", 48),
		"agent_id":                  fixture.authority.AgentID,
		"bench_version":             12,
		"run_row_id":                fixture.authority.RunRowID,
		"ticket_id":                 fixture.binding.TicketID,
		"ticket_deadline":           fixture.binding.Deadline,
		"coding_run_id":             fixture.authority.CodingRunID,
		"agent_artifact_sha256":     fixture.binding.AgentArtifactSHA256,
		"screened_image_sha256":     fixture.authority.ScreenedImageSHA256,
		"run_manifest_sha256":       fixture.authority.RunManifestSHA256,
		"task_set_manifest_sha256":  fixture.authority.TaskSetManifestSHA256,
		"authoring_evidence_sha256": fixture.authority.EvidenceSHA256,
		"evidence": map[string]any{
			"authoring_transcript_sha256": fixture.transcript.SHA256,
			"frozen_patch_sha256":         fixture.frozen.FrozenPatchSHA256,
		},
		"authoring_transcript_object_key": fixture.transcript.ObjectKey,
		"authoring_transcript_bytes":      fixture.transcript.SizeBytes,
		"authoring_event_count":           fixture.transcript.Events,
		"frozen_submission_object_key":    fixture.frozen.ObjectKey,
		"signature":                       strings.Repeat("a", 128),
	})
	if err != nil {
		t.Fatal(err)
	}
	return append(body, '\n')
}

func (fixture publicationFixture) authoringAcknowledgement(t *testing.T) []byte {
	t.Helper()
	body, err := json.Marshal(map[string]any{
		"freeze_id":                 "33333333-3333-4333-8333-333333333333",
		"agent_id":                  fixture.authority.AgentID,
		"run_row_id":                fixture.authority.RunRowID,
		"ticket_id":                 fixture.binding.TicketID,
		"coding_run_id":             fixture.authority.CodingRunID,
		"authoring_evidence_sha256": fixture.authority.EvidenceSHA256,
		"frozen_at":                 fixture.clock.now,
		"accepted":                  true,
		"idempotent":                false,
		"weight_eligible":           false,
	})
	if err != nil {
		t.Fatal(err)
	}
	return append(body, '\n')
}

func (fixture publicationFixture) terminalAuthority() PublicationAuthority {
	authority := fixture.authority
	authority.EvidenceSHA256 = strings.Repeat("9", 64)
	return authority
}

func (fixture publicationFixture) terminalRequest(t *testing.T) []byte {
	t.Helper()
	authority := fixture.terminalAuthority()
	body, err := json.Marshal(map[string]any{
		"validator_hotkey":      strings.Repeat("5", 48),
		"bench_version":         12,
		"run_row_id":            authority.RunRowID,
		"ticket_id":             fixture.binding.TicketID,
		"ticket_deadline":       fixture.binding.Deadline,
		"agent_artifact_sha256": fixture.binding.AgentArtifactSHA256,
		"screened_image_sha256": authority.ScreenedImageSHA256,
		"run_evidence_sha256":   authority.EvidenceSHA256,
		"evidence": map[string]any{
			"coding_run_id":            authority.CodingRunID,
			"validator_ticket_id":      fixture.binding.TicketID,
			"run_manifest_sha256":      authority.RunManifestSHA256,
			"task_set_manifest_sha256": authority.TaskSetManifestSHA256,
		},
		"signature": strings.Repeat("b", 128),
	})
	if err != nil {
		t.Fatal(err)
	}
	return append(body, '\n')
}

func (fixture publicationFixture) terminalAcknowledgement(t *testing.T) []byte {
	t.Helper()
	authority := fixture.terminalAuthority()
	body, err := json.Marshal(map[string]any{
		"agent_id":        authority.AgentID,
		"run_row_id":      authority.RunRowID,
		"ticket_id":       fixture.binding.TicketID,
		"coding_run_id":   authority.CodingRunID,
		"accepted":        true,
		"idempotent":      false,
		"weight_eligible": false,
	})
	if err != nil {
		t.Fatal(err)
	}
	return append(body, '\n')
}

func reopenPublicationStore(t *testing.T, fixture publicationFixture) (*Store, *Attempt) {
	t.Helper()
	store, err := Open(Config{
		Root: fixture.root, MaxTotalBytes: 512 << 20, MaxAttempts: 64,
		FinalizationGrace: time.Minute, OrphanGrace: time.Minute,
		ReleasedRetention: time.Minute, ExpiredRetention: time.Minute, Now: fixture.clock.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	attempt, err := store.Reserve(t.Context(), fixture.binding, fixture.limits)
	if err != nil {
		t.Fatal(err)
	}
	return store, attempt
}

func readPublicationBytes(t *testing.T, reader io.ReadCloser) []byte {
	t.Helper()
	defer reader.Close()
	body, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestShadowPublicationLifecycleSurvivesRestartAndGatesRelease(t *testing.T) {
	fixture := newPublicationFixture(t, "7")
	if got := fixture.store.records[fixture.attempt.ID()].ReservedBytes; got !=
		reservationForLimits(fixture.limits)+publicationReserveBytes {
		t.Fatalf("shadow reservation=%d", got)
	}
	if _, err := fixture.attempt.PrepareTerminalPublication(
		t.Context(), fixture.terminalAuthority(), fixture.terminalRequest(t),
	); !errors.Is(err, ErrState) {
		t.Fatalf("terminal before freeze acknowledgement err=%v", err)
	}
	authoringBody := fixture.authoringRequest(t)
	authoringArtifact, err := fixture.attempt.PrepareAuthoringPublication(
		t.Context(), fixture.authority, authoringBody,
	)
	if err != nil {
		t.Fatal(err)
	}
	if authoringArtifact != publicationArtifact(authoringBody) {
		t.Fatalf("authoring artifact=%#v", authoringArtifact)
	}
	pending, err := fixture.store.PendingPublications(t.Context(), 10)
	if err != nil || len(pending) != 1 || pending[0].Stage != PublicationAuthoringFreeze ||
		pending[0].RecordID != fixture.attempt.ID() {
		t.Fatalf("authoring pending=%#v err=%v", pending, err)
	}
	if body, err := json.Marshal(pending[0]); err == nil || body != nil ||
		strings.Contains(pending[0].String(), fixture.binding.TicketID) {
		t.Fatalf("pending publication diagnostics body=%q err=%v text=%q", body, err, pending[0].String())
	}
	reader, err := fixture.store.OpenPublication(t.Context(), fixture.attempt.ID(), PublicationAuthoringFreeze)
	if err != nil || !bytes.Equal(readPublicationBytes(t, reader), authoringBody) {
		t.Fatalf("authoring request replay err=%v", err)
	}
	if err := fixture.store.Close(); err != nil {
		t.Fatal(err)
	}
	fixture.store, fixture.attempt = reopenPublicationStore(t, fixture)
	pending, err = fixture.store.PendingPublications(t.Context(), 10)
	if err != nil || len(pending) != 1 || pending[0].Request != authoringArtifact {
		t.Fatalf("restarted authoring pending=%#v err=%v", pending, err)
	}

	authoringAck := fixture.authoringAcknowledgement(t)
	if _, err := fixture.attempt.AcknowledgeAuthoringPublication(
		t.Context(), authoringArtifact.SHA256, authoringAck,
	); err != nil {
		t.Fatal(err)
	}
	if err := fixture.store.Release(
		t.Context(), fixture.attempt.ID(), fixture.terminalAuthority().EvidenceSHA256,
	); !errors.Is(err, ErrState) {
		t.Fatalf("release after authoring midpoint err=%v", err)
	}
	evidenceRecords, err := fixture.store.Pending(t.Context(), 10)
	if err != nil || len(evidenceRecords) != 1 || evidenceRecords[0].AuthoringPublication == nil ||
		evidenceRecords[0].AuthoringPublication.RemoteAuthorityID != "33333333-3333-4333-8333-333333333333" {
		t.Fatalf("authoring acknowledgement record=%#v err=%v", evidenceRecords, err)
	}
	pending, err = fixture.store.PendingPublications(t.Context(), 10)
	if err != nil || len(pending) != 0 {
		t.Fatalf("pending after authoring ack=%#v err=%v", pending, err)
	}

	terminalBody := fixture.terminalRequest(t)
	terminalArtifact, err := fixture.attempt.PrepareTerminalPublication(
		t.Context(), fixture.terminalAuthority(), terminalBody,
	)
	if err != nil {
		t.Fatal(err)
	}
	pending, err = fixture.store.PendingPublications(t.Context(), 10)
	if err != nil || len(pending) != 1 || pending[0].Stage != PublicationTerminalResult ||
		pending[0].Request != terminalArtifact {
		t.Fatalf("terminal pending=%#v err=%v", pending, err)
	}
	terminalAck := fixture.terminalAcknowledgement(t)
	if _, err := fixture.attempt.AcknowledgeTerminalPublication(
		t.Context(), terminalArtifact.SHA256, terminalAck,
	); err != nil {
		t.Fatal(err)
	}
	if err := fixture.store.Release(
		t.Context(), fixture.attempt.ID(), strings.Repeat("8", 64),
	); !errors.Is(err, ErrConflict) {
		t.Fatalf("changed terminal evidence release err=%v", err)
	}
	if err := fixture.store.Release(
		t.Context(), fixture.attempt.ID(), fixture.terminalAuthority().EvidenceSHA256,
	); err != nil {
		t.Fatal(err)
	}
	if err := fixture.store.Close(); err != nil {
		t.Fatal(err)
	}

	fixture.store, fixture.attempt = reopenPublicationStore(t, fixture)
	records, err := fixture.store.Pending(t.Context(), 10)
	if err != nil || len(records) != 0 {
		t.Fatalf("released evidence pending=%#v err=%v", records, err)
	}
	reader, err = fixture.store.OpenPublicationAcknowledgement(
		t.Context(), fixture.attempt.ID(), PublicationTerminalResult,
	)
	if err != nil || !bytes.Equal(readPublicationBytes(t, reader), terminalAck) {
		t.Fatalf("terminal acknowledgement replay err=%v", err)
	}
	if err := fixture.store.Release(
		t.Context(), fixture.attempt.ID(), fixture.terminalAuthority().EvidenceSHA256,
	); err != nil {
		t.Fatalf("idempotent release: %v", err)
	}
	fixture.clock.now = fixture.clock.now.Add(2 * time.Minute)
	report, err := fixture.store.Sweep(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if report.DeletedRecords != 1 || report.DeletedObjects != 6 {
		t.Fatalf("publication retention sweep=%#v", report)
	}
	if err := fixture.store.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestCertificationPurposeCannotAcquireShadowPublicationCapacity(t *testing.T) {
	store, clock, _ := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "c")
	limits := codingrunner.DefaultLimits()
	attempt, err := store.Reserve(t.Context(), binding, limits)
	if err != nil {
		t.Fatal(err)
	}
	if got := store.records[attempt.ID()].ReservedBytes; got != reservationForLimits(limits) {
		t.Fatalf("certification reservation=%d", got)
	}
	authority := PublicationAuthority{
		AgentID: "11111111-1111-4111-8111-111111111111", BenchVersion: 12,
		RunRowID: "22222222-2222-4222-8222-222222222222", CodingRunID: "certification-c",
		ScreenedImageSHA256: strings.Repeat("d", 64), RunManifestSHA256: binding.AuthoritySHA256,
		TaskSetManifestSHA256: strings.Repeat("f", 64), EvidenceSHA256: strings.Repeat("c", 64),
	}
	if _, err := attempt.PrepareTerminalPublication(t.Context(), authority, []byte(`{}`)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("certification acquired shadow publication: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestPublicationReplayAndAuthorityDriftFailClosed(t *testing.T) {
	fixture := newPublicationFixture(t, "8")
	body := fixture.authoringRequest(t)
	original := append([]byte(nil), body...)
	oversized := make([]byte, maximumPublicationRequestBytes+1)
	if _, err := fixture.attempt.PrepareAuthoringPublication(
		t.Context(), fixture.authority, oversized,
	); !errors.Is(err, ErrInvalid) {
		t.Fatalf("oversized publication err=%v", err)
	}
	duplicate := []byte(`{"validator_hotkey":"one","validator_hotkey":"two"}`)
	if _, err := fixture.attempt.PrepareAuthoringPublication(
		t.Context(), fixture.authority, duplicate,
	); !errors.Is(err, ErrInvalid) {
		t.Fatalf("duplicate publication fields err=%v", err)
	}
	artifact, err := fixture.attempt.PrepareAuthoringPublication(t.Context(), fixture.authority, body)
	if err != nil {
		t.Fatal(err)
	}
	body[0] ^= 1
	reader, err := fixture.store.OpenPublication(
		t.Context(), fixture.attempt.ID(), PublicationAuthoringFreeze,
	)
	if err != nil || !bytes.Equal(readPublicationBytes(t, reader), original) {
		t.Fatalf("caller mutation changed stored request: %v", err)
	}
	again, err := fixture.attempt.PrepareAuthoringPublication(t.Context(), fixture.authority, original)
	if err != nil || again != artifact {
		t.Fatalf("exact request replay=%#v err=%v", again, err)
	}
	changed := append([]byte(nil), original...)
	changed = append(bytes.TrimSpace(changed), ' ')
	if _, err := fixture.attempt.PrepareAuthoringPublication(
		t.Context(), fixture.authority, changed,
	); !errors.Is(err, ErrConflict) {
		t.Fatalf("changed request err=%v", err)
	}
	drifted := fixture.authority
	drifted.CodingRunID = "coding-run-other"
	if _, err := fixture.attempt.PrepareAuthoringPublication(
		t.Context(), drifted, original,
	); !errors.Is(err, ErrConflict) {
		t.Fatalf("authority drift err=%v", err)
	}
	acknowledgement := fixture.authoringAcknowledgement(t)
	if _, err := fixture.attempt.AcknowledgeAuthoringPublication(
		t.Context(), strings.Repeat("0", 64), acknowledgement,
	); !errors.Is(err, ErrConflict) {
		t.Fatalf("foreign request acknowledgement err=%v", err)
	}
	ackArtifact, err := fixture.attempt.AcknowledgeAuthoringPublication(
		t.Context(), artifact.SHA256, acknowledgement,
	)
	if err != nil {
		t.Fatal(err)
	}
	againAck, err := fixture.attempt.AcknowledgeAuthoringPublication(
		t.Context(), artifact.SHA256, acknowledgement,
	)
	if err != nil || againAck != ackArtifact {
		t.Fatalf("exact acknowledgement replay=%#v err=%v", againAck, err)
	}
	var shape map[string]any
	if err := json.Unmarshal(acknowledgement, &shape); err != nil {
		t.Fatal(err)
	}
	shape["freeze_id"] = "44444444-4444-4444-8444-444444444444"
	changedAck, _ := json.Marshal(shape)
	if _, err := fixture.attempt.AcknowledgeAuthoringPublication(
		t.Context(), artifact.SHA256, changedAck,
	); !errors.Is(err, ErrConflict) {
		t.Fatalf("changed acknowledgement err=%v", err)
	}
	terminalDrift := fixture.terminalAuthority()
	terminalDrift.RunManifestSHA256 = strings.Repeat("7", 64)
	if _, err := fixture.attempt.PrepareTerminalPublication(
		t.Context(), terminalDrift, fixture.terminalRequest(t),
	); !errors.Is(err, ErrConflict) {
		t.Fatalf("cross-stage authority drift err=%v", err)
	}
	if err := fixture.store.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestConcurrentPublicationPreparationHasOneCoherentWinner(t *testing.T) {
	fixture := newPublicationFixture(t, "b")
	first := fixture.authoringRequest(t)
	second := append(bytes.TrimSpace(append([]byte(nil), first...)), ' ', '\n')
	type result struct {
		artifact PublicationArtifact
		err      error
	}
	start := make(chan struct{})
	results := make(chan result, 2)
	var wait sync.WaitGroup
	for _, body := range [][]byte{first, second} {
		body := body
		wait.Add(1)
		go func() {
			defer wait.Done()
			<-start
			artifact, err := fixture.attempt.PrepareAuthoringPublication(
				t.Context(), fixture.authority, body,
			)
			results <- result{artifact: artifact, err: err}
		}()
	}
	close(start)
	wait.Wait()
	close(results)
	successes, conflicts := 0, 0
	var winner PublicationArtifact
	for observed := range results {
		switch {
		case observed.err == nil:
			successes++
			winner = observed.artifact
		case errors.Is(observed.err, ErrConflict):
			conflicts++
		default:
			t.Fatalf("concurrent publication err=%v", observed.err)
		}
	}
	if successes != 1 || conflicts != 1 {
		t.Fatalf("concurrent results success=%d conflict=%d", successes, conflicts)
	}
	pending, err := fixture.store.PendingPublications(t.Context(), 1)
	if err != nil || len(pending) != 1 || pending[0].Request != winner {
		t.Fatalf("concurrent winner pending=%#v err=%v winner=%#v", pending, err, winner)
	}
	if err := fixture.store.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestTerminalWithoutPatchPublishesWithoutAuthoringFreeze(t *testing.T) {
	store, clock, _ := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "9")
	binding.Purpose = PurposeShadowAttempt
	binding.ExecutionID = "shadow-attempt-9"
	binding.HarnessAuthoritySHA256 = strings.Repeat("e", 64)
	binding.ScreenedImageSHA256 = strings.Repeat("d", 64)
	limits := codingrunner.DefaultLimits()
	attempt, err := store.Reserve(t.Context(), binding, limits)
	if err != nil {
		t.Fatal(err)
	}
	transcript := commitFixtureTranscript(t, attempt, []byte("{\"sequence\":1}\n"))
	submission := fixtureSubmission(t, binding, transcript)
	failure := codingrunner.FreezeFailure{
		Kind: "repair_failure", Code: "protected_path", BaseTreeSHA256: submission.BaseTreeSHA256,
		VisibleBundleSHA256: submission.VisibleBundleSHA256, FinalTreeSHA256: submission.FinalTreeSHA256,
		ChangedPathRoot: submission.ChangedPathRoot, AuthoringEventRoot: submission.AuthoringEventRoot,
		AuthoringTranscriptSHA256: transcript.SHA256, AuthoringTranscriptBytes: transcript.SizeBytes,
		ProtectedPathsIntact: false,
	}
	if _, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Failure: &failure}); err != nil {
		t.Fatal(err)
	}
	fixture := publicationFixture{
		store: store, clock: clock, binding: binding, attempt: attempt, transcript: transcript, limits: limits,
		authority: PublicationAuthority{
			AgentID: "11111111-1111-4111-8111-111111111111", BenchVersion: 12,
			RunRowID:              "22222222-2222-4222-8222-222222222222",
			CodingRunID:           "coding-run-9",
			ScreenedImageSHA256:   strings.Repeat("d", 64),
			RunManifestSHA256:     binding.AuthoritySHA256,
			TaskSetManifestSHA256: strings.Repeat("f", 64),
			EvidenceSHA256:        strings.Repeat("c", 64),
		},
	}
	if _, err := attempt.PrepareAuthoringPublication(
		t.Context(), fixture.authority, fixture.authoringRequest(t),
	); !errors.Is(err, ErrState) {
		t.Fatalf("terminal failure accepted authoring publication: %v", err)
	}
	terminalArtifact, err := attempt.PrepareTerminalPublication(
		t.Context(), fixture.terminalAuthority(), fixture.terminalRequest(t),
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := attempt.AcknowledgeTerminalPublication(
		t.Context(), terminalArtifact.SHA256, fixture.terminalAcknowledgement(t),
	); err != nil {
		t.Fatal(err)
	}
	if err := store.Release(t.Context(), attempt.ID(), fixture.terminalAuthority().EvidenceSHA256); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestPublicationObjectCorruptionFailsRestart(t *testing.T) {
	fixture := newPublicationFixture(t, "a")
	body := fixture.authoringRequest(t)
	artifact, err := fixture.attempt.PrepareAuthoringPublication(t.Context(), fixture.authority, body)
	if err != nil {
		t.Fatal(err)
	}
	if err := fixture.store.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(fixture.root, "objects", "sha256", artifact.SHA256[:2], artifact.SHA256[2:])
	corrupt := append([]byte(nil), body...)
	corrupt[0] ^= 1
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, corrupt, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o400); err != nil {
		t.Fatal(err)
	}
	store, err := Open(Config{
		Root: fixture.root, MaxTotalBytes: 512 << 20, MaxAttempts: 64,
		FinalizationGrace: time.Minute, OrphanGrace: time.Minute,
		ReleasedRetention: time.Minute, ExpiredRetention: time.Minute, Now: fixture.clock.Now,
	})
	if err == nil {
		_ = store.Close()
		t.Fatal("corrupt publication object was accepted")
	}
	if !errors.Is(err, ErrCorrupt) {
		t.Fatalf("corrupt publication err=%v", err)
	}
}

func TestPlatformPublicationVectorsMatchJournalBoundary(t *testing.T) {
	type vector struct {
		AgentID  string          `json:"agent_id"`
		Request  json.RawMessage `json:"request"`
		Response json.RawMessage `json:"response"`
	}
	load := func(name string) vector {
		t.Helper()
		body, err := os.ReadFile(filepath.Join(
			"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata", name,
		))
		if err != nil {
			t.Fatal(err)
		}
		var value vector
		if err := json.Unmarshal(body, &value); err != nil {
			t.Fatal(err)
		}
		return value
	}

	authoring := load("coding_authoring_freeze_v1.json")
	var authoringRequest authoringFreezeRequest
	if err := json.Unmarshal(authoring.Request, &authoringRequest); err != nil {
		t.Fatal(err)
	}
	authoringRecord := &Record{
		Binding: Binding{
			Purpose: PurposeShadowAttempt, TicketID: authoringRequest.TicketID,
			AgentArtifactSHA256:    authoringRequest.AgentArtifactSHA256,
			AuthoritySHA256:        authoringRequest.RunManifestSHA256,
			HarnessAuthoritySHA256: strings.Repeat("e", 64),
			ScreenedImageSHA256:    authoringRequest.ScreenedImageSHA256,
			Deadline:               authoringRequest.TicketDeadline,
		},
		State: StateReady,
		Transcript: &TranscriptArtifact{
			ObjectKey: authoringRequest.AuthoringTranscriptObjectKey,
			SHA256:    authoringRequest.Evidence.AuthoringTranscriptSHA256,
			SizeBytes: authoringRequest.AuthoringTranscriptBytes,
			Events:    authoringRequest.AuthoringEventCount,
		},
		Frozen: &FrozenRecord{Artifact: FrozenArtifact{
			ObjectKey:         authoringRequest.FrozenSubmissionObjectKey,
			FrozenPatchSHA256: authoringRequest.Evidence.FrozenPatchSHA256,
		}},
	}
	authoringAuthority := PublicationAuthority{
		AgentID: authoring.AgentID, BenchVersion: authoringRequest.BenchVersion,
		RunRowID:              authoringRequest.RunRowID,
		CodingRunID:           authoringRequest.CodingRunID,
		ScreenedImageSHA256:   authoringRequest.ScreenedImageSHA256,
		RunManifestSHA256:     authoringRequest.RunManifestSHA256,
		TaskSetManifestSHA256: authoringRequest.TaskSetManifestSHA256,
		EvidenceSHA256:        authoringRequest.AuthoringEvidenceSHA256,
	}
	if err := validatePublicationRequest(
		PublicationAuthoringFreeze, authoringRecord, authoringAuthority, authoring.Request,
	); err != nil {
		t.Fatalf("authoring vector request: %v", err)
	}
	publication := PublicationRecord{Stage: PublicationAuthoringFreeze, Authority: authoringAuthority}
	if _, err := validatePublicationAcknowledgement(
		PublicationAuthoringFreeze, authoringRecord, publication, authoring.Response,
	); err != nil {
		t.Fatalf("authoring vector response: %v", err)
	}

	terminal := load("coding_shadow_result_submission_v1.json")
	var terminalRequest terminalResultRequest
	if err := json.Unmarshal(terminal.Request, &terminalRequest); err != nil {
		t.Fatal(err)
	}
	terminalRecord := &Record{Binding: Binding{
		Purpose: PurposeShadowAttempt, TicketID: terminalRequest.TicketID,
		AgentArtifactSHA256:    terminalRequest.AgentArtifactSHA256,
		AuthoritySHA256:        terminalRequest.Evidence.RunManifestSHA256,
		HarnessAuthoritySHA256: strings.Repeat("e", 64),
		ScreenedImageSHA256:    terminalRequest.ScreenedImageSHA256,
		Deadline:               terminalRequest.TicketDeadline,
	}, State: StateTerminalWithoutPatch}
	terminalAuthority := PublicationAuthority{
		AgentID: terminal.AgentID, BenchVersion: terminalRequest.BenchVersion,
		RunRowID:              terminalRequest.RunRowID,
		CodingRunID:           terminalRequest.Evidence.CodingRunID,
		ScreenedImageSHA256:   terminalRequest.ScreenedImageSHA256,
		RunManifestSHA256:     terminalRequest.Evidence.RunManifestSHA256,
		TaskSetManifestSHA256: terminalRequest.Evidence.TaskSetManifestSHA256,
		EvidenceSHA256:        terminalRequest.RunEvidenceSHA256,
	}
	if err := validatePublicationRequest(
		PublicationTerminalResult, terminalRecord, terminalAuthority, terminal.Request,
	); err != nil {
		t.Fatalf("terminal vector request: %v", err)
	}
	publication = PublicationRecord{Stage: PublicationTerminalResult, Authority: terminalAuthority}
	if _, err := validatePublicationAcknowledgement(
		PublicationTerminalResult, terminalRecord, publication, terminal.Response,
	); err != nil {
		t.Fatalf("terminal vector response: %v", err)
	}
}
