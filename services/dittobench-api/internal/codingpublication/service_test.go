package codingpublication

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingevidence"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

const fixtureControlToken = "coding-publication-control-token-0000000000000001"

type publicationFixture struct {
	store          *codingoutbox.Store
	service        *Service
	ticketID       string
	authority      codingoutbox.PublicationAuthority
	transcript     codingoutbox.TranscriptArtifact
	transcriptBody []byte
	request        []byte
	ack            []byte
}

func newPublicationFixture(t *testing.T) publicationFixture {
	t.Helper()
	now := time.Date(2026, 8, 23, 20, 0, 0, 0, time.UTC)
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	store, err := codingoutbox.Open(codingoutbox.Config{
		Root: root, MaxTotalBytes: 512 << 20, MaxAttempts: 16,
		FinalizationGrace: time.Minute, OrphanGrace: time.Minute,
		ReleasedRetention: time.Minute, ExpiredRetention: time.Minute,
		Now: func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	ticketID := "33333333-3333-4333-8333-333333333333"
	binding := codingoutbox.Binding{
		Purpose: codingoutbox.PurposeShadowAttempt, ExecutionID: ticketID,
		AgentArtifactSHA256: strings.Repeat("a", 64), HarnessInstanceID: "harness-publication-001",
		AuthoritySHA256: strings.Repeat("b", 64), HarnessAuthoritySHA256: strings.Repeat("c", 64),
		ScreenedImageSHA256: strings.Repeat("d", 64), TicketID: ticketID,
		CaseID: "case-publication-001", ProfileCapabilityID: "profile-publication-001",
		Deadline: now.Add(time.Hour),
	}
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	writer, err := attempt.BeginTranscript(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	transcriptBody := []byte("{\"sequence\":1}\n")
	if _, err := writer.Write(transcriptBody); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(transcriptBody)
	transcript, err := writer.Commit(t.Context(), codingrunner.TranscriptIdentity{
		SHA256: hex.EncodeToString(digest[:]), SizeBytes: int64(len(transcriptBody)), Events: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	failure := codingrunner.FreezeFailure{
		Kind: string(codingcontract.DomainValidatorInfrastructure), Code: "authoring_infrastructure",
		BaseTreeSHA256: strings.Repeat("1", 64), VisibleBundleSHA256: strings.Repeat("2", 64),
		FinalTreeSHA256: strings.Repeat("3", 64), ChangedPathRoot: strings.Repeat("4", 64),
		AuthoringEventRoot:        strings.Repeat("5", 64),
		AuthoringTranscriptSHA256: transcript.SHA256, AuthoringTranscriptBytes: transcript.SizeBytes,
		ProtectedPathsIntact: true,
	}
	if _, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Failure: &failure}); err != nil {
		t.Fatal(err)
	}
	authority := codingoutbox.PublicationAuthority{
		AgentID: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", BenchVersion: 12,
		RunRowID: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", CodingRunID: "coding-run-publication-001",
		ScreenedImageSHA256: binding.ScreenedImageSHA256, RunManifestSHA256: binding.AuthoritySHA256,
		TaskSetManifestSHA256: strings.Repeat("e", 64), EvidenceSHA256: strings.Repeat("f", 64),
	}
	request := mustJSON(t, map[string]any{
		"validator_hotkey": strings.Repeat("5", 48), "bench_version": 12,
		"run_row_id": authority.RunRowID, "ticket_id": ticketID, "ticket_deadline": binding.Deadline,
		"agent_artifact_sha256": binding.AgentArtifactSHA256,
		"screened_image_sha256": authority.ScreenedImageSHA256,
		"run_evidence_sha256":   authority.EvidenceSHA256,
		"evidence": map[string]any{
			"coding_run_id": authority.CodingRunID, "validator_ticket_id": ticketID,
			"run_manifest_sha256":      authority.RunManifestSHA256,
			"task_set_manifest_sha256": authority.TaskSetManifestSHA256,
		},
		"signature": strings.Repeat("9", 128),
	})
	ack := mustJSON(t, map[string]any{
		"agent_id": authority.AgentID, "run_row_id": authority.RunRowID,
		"ticket_id": ticketID, "coding_run_id": authority.CodingRunID,
		"accepted": true, "idempotent": false, "weight_eligible": false,
	})
	service, err := New(Config{Store: store, ControlToken: fixtureControlToken})
	if err != nil {
		t.Fatal(err)
	}
	return publicationFixture{
		store: store, service: service, ticketID: ticketID,
		authority: authority, transcript: transcript, transcriptBody: transcriptBody,
		request: request, ack: ack,
	}
}

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	body, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return append(body, '\n')
}

func terminalFinalization(
	ticketID string,
	acknowledgement codingoutbox.PublicationArtifact,
) codingevidence.WireFinalization {
	return codingevidence.WireFinalization{
		Schema:                "dittobench-coding-sealed-evidence-finalized-v1",
		CodingContractVersion: 1, WeightEligible: false,
		TicketID: ticketID, ClaimGeneration: 3,
		UploadID:     "44444444-4444-4444-8444-444444444444",
		EvidenceKind: codingevidence.KindTerminalPublicationAcknowledgement,
		SHA256:       acknowledgement.SHA256, SizeBytes: acknowledgement.SizeBytes,
		FinalizedAt: time.Date(2026, 8, 23, 20, 1, 0, 0, time.UTC), Accepted: true,
	}
}

func invoke(t *testing.T, service *Service, operation string, command any, token string) *httptest.ResponseRecorder {
	t.Helper()
	body := mustJSON(t, command)
	request := httptest.NewRequest(http.MethodPost, "/v1/coding/publications/"+operation, bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+token)
	response := httptest.NewRecorder()
	service.Handler().ServeHTTP(response, request)
	return response
}

func decodeResult(t *testing.T, response *httptest.ResponseRecorder) result {
	t.Helper()
	var value result
	if err := json.Unmarshal(response.Body.Bytes(), &value); err != nil {
		t.Fatal(err)
	}
	return value
}

func invokeEvidence(
	t *testing.T,
	service *Service,
	command evidenceOpenCommand,
	token string,
) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(
		http.MethodPost,
		"/v1/coding/evidence/open",
		bytes.NewReader(mustJSON(t, command)),
	)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+token)
	response := httptest.NewRecorder()
	service.EvidenceHandler().ServeHTTP(response, request)
	return response
}

func TestPublicationServiceDurablyPreparesReplaysAndAcknowledges(t *testing.T) {
	fixture := newPublicationFixture(t)
	prepare := map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"stage": codingoutbox.PublicationTerminalResult, "authority": fixture.authority,
		"body_base64": base64.StdEncoding.EncodeToString(fixture.request),
	}
	response := invoke(t, fixture.service, "prepare", prepare, fixtureControlToken)
	if response.Code != http.StatusOK {
		t.Fatalf("prepare status=%d body=%s", response.Code, response.Body.String())
	}
	prepared := decodeResult(t, response)
	if prepared.Artifact == nil || prepared.RecordID == "" {
		t.Fatalf("prepared=%#v", prepared)
	}
	response = invoke(t, fixture.service, "lookup", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"stage": codingoutbox.PublicationTerminalResult,
	}, fixtureControlToken)
	lookedUp := decodeResult(t, response)
	if response.Code != http.StatusOK || lookedUp.Publication == nil ||
		lookedUp.Publication.RecordID != prepared.RecordID ||
		lookedUp.Publication.Request != *prepared.Artifact ||
		lookedUp.Publication.Acknowledgement != nil {
		t.Fatalf("lookup=%#v status=%d", lookedUp, response.Code)
	}
	response = invoke(t, fixture.service, "pending", map[string]any{
		"schema": commandSchema, "limit": 10,
	}, fixtureControlToken)
	pending := decodeResult(t, response)
	if response.Code != http.StatusOK || len(pending.Pending) != 1 ||
		pending.Pending[0].Request != *prepared.Artifact {
		t.Fatalf("pending=%#v status=%d", pending, response.Code)
	}
	prematureDigest := sha256.Sum256(fixture.ack)
	prematureSHA256 := hex.EncodeToString(prematureDigest[:])
	prematureAck := codingoutbox.PublicationArtifact{
		ObjectKey: "sha256/" + prematureSHA256,
		SHA256:    prematureSHA256, SizeBytes: int64(len(fixture.ack)),
	}
	response = invoke(t, fixture.service, "release", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"record_id":                prepared.RecordID,
		"terminal_evidence_sha256": fixture.authority.EvidenceSHA256,
		"finalization":             terminalFinalization(fixture.ticketID, prematureAck),
	}, fixtureControlToken)
	if response.Code != http.StatusConflict {
		t.Fatalf("premature release status=%d body=%s", response.Code, response.Body.String())
	}
	response = invoke(t, fixture.service, "open", map[string]any{
		"schema": commandSchema, "record_id": prepared.RecordID,
		"stage": codingoutbox.PublicationTerminalResult, "acknowledgement": false,
	}, fixtureControlToken)
	opened := decodeResult(t, response)
	raw, err := base64.StdEncoding.Strict().DecodeString(opened.BodyBase64)
	if err != nil || !bytes.Equal(raw, fixture.request) {
		t.Fatalf("open err=%v", err)
	}
	response = invoke(t, fixture.service, "acknowledge", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"stage":          codingoutbox.PublicationTerminalResult,
		"request_sha256": prepared.Artifact.SHA256,
		"body_base64":    base64.StdEncoding.EncodeToString(fixture.ack),
	}, fixtureControlToken)
	acknowledged := decodeResult(t, response)
	if response.Code != http.StatusOK || acknowledged.Artifact == nil {
		t.Fatalf("acknowledged=%#v status=%d", acknowledged, response.Code)
	}
	_, record, err := fixture.store.Lookup(
		t.Context(), codingoutbox.PurposeShadowAttempt, fixture.ticketID,
	)
	if err != nil || record.State != codingoutbox.StateTerminalWithoutPatch ||
		record.ReleaseFinalization != nil {
		t.Fatalf("acknowledgement released before finalization record=%#v err=%v", record, err)
	}
	response = invoke(t, fixture.service, "lookup", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"stage": codingoutbox.PublicationTerminalResult,
	}, fixtureControlToken)
	lookedUp = decodeResult(t, response)
	if lookedUp.Publication == nil || lookedUp.Publication.Acknowledgement == nil ||
		*lookedUp.Publication.Acknowledgement != *acknowledged.Artifact {
		t.Fatalf("acknowledged lookup=%#v", lookedUp)
	}
	response = invoke(t, fixture.service, "pending", map[string]any{
		"schema": commandSchema, "limit": 10,
	}, fixtureControlToken)
	if value := decodeResult(t, response); len(value.Pending) != 0 {
		t.Fatalf("pending after ack=%#v", value.Pending)
	}
	finalization := terminalFinalization(fixture.ticketID, *acknowledged.Artifact)
	response = invoke(t, fixture.service, "release", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"record_id":                prepared.RecordID,
		"terminal_evidence_sha256": fixture.authority.EvidenceSHA256,
		"finalization":             finalization,
	}, fixtureControlToken)
	if response.Code != http.StatusOK || decodeResult(t, response).RecordID != prepared.RecordID {
		t.Fatalf("release status=%d body=%s", response.Code, response.Body.String())
	}
	_, record, err = fixture.store.Lookup(
		t.Context(), codingoutbox.PurposeShadowAttempt, fixture.ticketID,
	)
	if err != nil || record.State != codingoutbox.StateReleased ||
		record.ReleaseFinalization == nil ||
		record.ReleaseFinalization.UploadID != finalization.UploadID ||
		record.ReleaseFinalization.ClaimGeneration != finalization.ClaimGeneration {
		t.Fatalf("finalized release record=%#v err=%v", record, err)
	}
	finalization.Idempotent = true
	response = invoke(t, fixture.service, "release", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"record_id":                prepared.RecordID,
		"terminal_evidence_sha256": fixture.authority.EvidenceSHA256,
		"finalization":             finalization,
	}, fixtureControlToken)
	if response.Code != http.StatusOK {
		t.Fatalf("idempotent release status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestEvidenceServiceStreamsExactReleasedObjects(t *testing.T) {
	fixture := newPublicationFixture(t)
	preparedResponse := invoke(t, fixture.service, "prepare", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"stage": codingoutbox.PublicationTerminalResult, "authority": fixture.authority,
		"body_base64": base64.StdEncoding.EncodeToString(fixture.request),
	}, fixtureControlToken)
	prepared := decodeResult(t, preparedResponse)
	if prepared.Artifact == nil || prepared.RecordID == "" {
		t.Fatalf("prepared=%#v", prepared)
	}
	ackResponse := invoke(t, fixture.service, "acknowledge", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"stage":          codingoutbox.PublicationTerminalResult,
		"request_sha256": prepared.Artifact.SHA256,
		"body_base64":    base64.StdEncoding.EncodeToString(fixture.ack),
	}, fixtureControlToken)
	acknowledged := decodeResult(t, ackResponse)
	if acknowledged.Artifact == nil {
		t.Fatalf("acknowledged=%#v", acknowledged)
	}
	releaseResponse := invoke(t, fixture.service, "release", map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"record_id":                prepared.RecordID,
		"terminal_evidence_sha256": fixture.authority.EvidenceSHA256,
		"finalization":             terminalFinalization(fixture.ticketID, *acknowledged.Artifact),
	}, fixtureControlToken)
	if releaseResponse.Code != http.StatusOK {
		t.Fatalf("release status=%d body=%s", releaseResponse.Code, releaseResponse.Body.String())
	}
	cases := []struct {
		kind codingevidence.Kind
		body []byte
	}{
		{codingevidence.KindAuthoringTranscript, fixture.transcriptBody},
		{codingevidence.KindTerminalPublicationRequest, fixture.request},
		{codingevidence.KindTerminalPublicationAcknowledgement, fixture.ack},
	}
	for _, expected := range cases {
		digest := sha256.Sum256(expected.body)
		command := evidenceOpenCommand{
			Schema: evidenceCommandSchema, TicketID: fixture.ticketID,
			RecordID: prepared.RecordID, EvidenceKind: expected.kind,
			SHA256: hex.EncodeToString(digest[:]), SizeBytes: int64(len(expected.body)),
		}
		response := invokeEvidence(t, fixture.service, command, fixtureControlToken)
		if response.Code != http.StatusOK || !bytes.Equal(response.Body.Bytes(), expected.body) ||
			response.Header().Get("Content-Type") != "application/octet-stream" ||
			response.Header().Get("Content-Length") != strconv.Itoa(len(expected.body)) ||
			response.Header().Get("X-Ditto-Evidence-Kind") != string(expected.kind) ||
			response.Header().Get("X-Ditto-Evidence-SHA256") != command.SHA256 {
			t.Fatalf("kind=%s status=%d headers=%v", expected.kind, response.Code, response.Header())
		}
	}
	manifestRequest := httptest.NewRequest(
		http.MethodPost,
		"/v1/coding/evidence/manifest",
		bytes.NewReader(mustJSON(t, evidenceManifestCommand{
			Schema:   evidenceManifestCommandSchema,
			TicketID: fixture.ticketID,
			RecordID: prepared.RecordID,
		})),
	)
	manifestRequest.Header.Set("Content-Type", "application/json")
	manifestRequest.Header.Set("Authorization", "Bearer "+fixtureControlToken)
	manifestResponse := httptest.NewRecorder()
	fixture.service.EvidenceHandler().ServeHTTP(manifestResponse, manifestRequest)
	var manifest evidenceManifestResult
	if err := json.Unmarshal(manifestResponse.Body.Bytes(), &manifest); err != nil ||
		manifestResponse.Code != http.StatusOK || manifest.Schema != evidenceManifestSchema ||
		manifest.TicketID != fixture.ticketID || manifest.RecordID != prepared.RecordID ||
		len(manifest.Evidence) != len(cases) {
		t.Fatalf("manifest=%#v status=%d err=%v", manifest, manifestResponse.Code, err)
	}
	drifted := evidenceOpenCommand{
		Schema: evidenceCommandSchema, TicketID: fixture.ticketID,
		RecordID: prepared.RecordID, EvidenceKind: codingevidence.KindAuthoringTranscript,
		SHA256: strings.Repeat("0", 64), SizeBytes: fixture.transcript.SizeBytes,
	}
	if response := invokeEvidence(t, fixture.service, drifted, fixtureControlToken); response.Code != http.StatusConflict {
		t.Fatalf("drift status=%d", response.Code)
	}
	if response := invokeEvidence(t, fixture.service, drifted, "wrong-control-token-000000000000000000"); response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthorized status=%d", response.Code)
	}
}

func TestPublicationServiceRejectsAuthMalformedAndAuthorityDrift(t *testing.T) {
	fixture := newPublicationFixture(t)
	command := map[string]any{
		"schema": commandSchema, "ticket_id": fixture.ticketID,
		"stage": codingoutbox.PublicationTerminalResult, "authority": fixture.authority,
		"body_base64": base64.StdEncoding.EncodeToString(fixture.request),
	}
	if response := invoke(t, fixture.service, "prepare", command, "wrong-control-token-000000000000000000"); response.Code != http.StatusUnauthorized {
		t.Fatalf("wrong token status=%d", response.Code)
	}
	drifted := command
	authority := fixture.authority
	authority.EvidenceSHA256 = strings.Repeat("0", 64)
	drifted["authority"] = authority
	if response := invoke(t, fixture.service, "prepare", drifted, fixtureControlToken); response.Code != http.StatusConflict {
		t.Fatalf("drift status=%d body=%s", response.Code, response.Body.String())
	}
	request := httptest.NewRequest(
		http.MethodPost,
		"/v1/coding/publications/prepare",
		strings.NewReader(`{"schema":"x","schema":"x"}`),
	)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+fixtureControlToken)
	response := httptest.NewRecorder()
	fixture.service.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("duplicate status=%d", response.Code)
	}
	if _, err := json.Marshal(fixture.service); !errors.Is(err, ErrInvalid) {
		t.Fatalf("marshal err=%v", err)
	}
	if strings.Contains(fixture.service.String(), fixtureControlToken) {
		t.Fatal("control token leaked")
	}
}

func TestPublicationServiceCloseRefusesInflightAndThenZerosAuthority(t *testing.T) {
	fixture := newPublicationFixture(t)
	fixture.service.mu.Lock()
	fixture.service.active = 1
	fixture.service.mu.Unlock()
	if err := fixture.service.Close(); !errors.Is(err, ErrClosed) {
		t.Fatalf("inflight close err=%v", err)
	}
	fixture.service.release()
	if err := fixture.service.Close(); err != nil {
		t.Fatal(err)
	}
	response := invoke(t, fixture.service, "pending", map[string]any{
		"schema": commandSchema, "limit": 1,
	}, fixtureControlToken)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("closed status=%d", response.Code)
	}
}
