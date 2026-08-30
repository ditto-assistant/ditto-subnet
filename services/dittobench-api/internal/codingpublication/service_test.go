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
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

const fixtureControlToken = "coding-publication-control-token-0000000000000001"

type publicationFixture struct {
	store     *codingoutbox.Store
	service   *Service
	ticketID  string
	authority codingoutbox.PublicationAuthority
	request   []byte
	ack       []byte
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
		authority: authority, request: request, ack: ack,
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
