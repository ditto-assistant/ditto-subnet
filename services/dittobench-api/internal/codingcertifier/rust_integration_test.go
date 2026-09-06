package codingcertifier

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

func TestRustHarnessCertificationIntegration(t *testing.T) {
	harnessURL := os.Getenv("DITTOBENCH_CODING_RUST_HARNESS_URL")
	if harnessURL == "" {
		t.Skip("DITTOBENCH_CODING_RUST_HARNESS_URL is not set")
	}
	fixture := newCertificationFixture(t)
	harness, err := NewHTTPHarnessClient(harnessURL, &http.Client{Timeout: 2 * time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	certifier, err := New(Config{
		Harness: harness, Publisher: fixture.publisher, Executor: fixture.graderExec,
		TranscriptSink: fixture.transcripts, FrozenSubmissionSink: fixture.frozen,
		InferenceEvidence: fixture.inference,
		OpenVisibleBundle: bytesOpener(fixture.visible), OpenGraderBundle: bytesOpener(fixture.grader),
		CertificationTTL: time.Hour,
	})
	if err != nil {
		t.Fatal(err)
	}
	receipt, err := certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Status != StatusCertified || receipt.AuthoringEventCount != 4 ||
		receipt.AuthoringTranscriptObjectKey == nil || receipt.ModelEvidence == nil {
		t.Fatalf("receipt=%#v", receipt)
	}
}

// This exercises the actual Rust service and native Go workspace on public
// scripted fixtures; it is not production model or private grading evidence.
func TestRustHostedHarnessV2Integration(t *testing.T) {
	harnessURL := os.Getenv("DITTOBENCH_CODING_RUST_HARNESS_URL")
	if harnessURL == "" {
		t.Skip("scripted Rust harness is not configured")
	}
	fixture := newCertificationFixture(t)
	authority := codingrunner.HostedAuthority{EvaluationID: "10000000-0000-4000-8000-000000000001", AttemptID: "20000000-0000-4000-8000-000000000002", AssignmentSHA256: strings.Repeat("a", 64)}
	manifest := fixture.request.RunnerManifest
	manifest.CodingContractVersion = 2
	manifest.TicketID, manifest.CaseID = authority.EvaluationID, authority.AttemptID
	session, err := codingrunner.NewHostedSession(t.Context(), authority, manifest, bytes.NewReader(fixture.visible), fixture.graderExec)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	workspace := httptest.NewServer(session.Handler())
	defer workspace.Close()
	client, err := NewHostedHTTPHarnessClient(harnessURL, &http.Client{Transport: &http.Transport{}, Timeout: 2 * time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	if health, err := client.Health(t.Context()); err != nil || !health.SupportsCodingV2() {
		t.Fatal("Rust v2 health failed")
	}
	seed := codingcontract.HostedSeedRequest(fixture.request.Seed)
	seed.CodingContractVersion = 2
	seed.TicketID, seed.CaseID = authority.EvaluationID, authority.AttemptID
	seed.Memories = []codingcontract.VisibleMemory{{MemoryID: "memory-1", Scope: "profile", Type: "user_workflow", Content: "Preserve leading whitespace.", Supersedes: []string{}, ConfidenceMicros: 900000}}
	seed.MemoryBundleSHA256 = canonicalMemoryDigest(t, seed.Memories)
	if _, err := client.Seed(t.Context(), seed); err != nil {
		t.Fatal(err)
	}
	if replay, err := client.Seed(t.Context(), seed); err != nil || !replay.IdempotentReplay {
		t.Fatal("Rust v2 seed replay failed")
	}
	run := codingcontract.HostedRunRequest(fixture.request.runRequest(workspace.URL + "/tool"))
	run.CodingContractVersion = 2
	run.TicketID, run.CaseID = seed.TicketID, seed.CaseID
	if _, err := client.Run(t.Context(), run); err != nil {
		t.Fatal(err)
	}
	frozen := session.Freeze()
	if frozen.Submission == nil || frozen.Submission.CodingContractVersion != 2 || len(frozen.Submission.Changes) != 1 {
		t.Fatalf("native freeze failed: %#v", frozen)
	}
	var transcript bytes.Buffer
	identity, err := session.WriteTranscript(&transcript)
	if err != nil || identity.Events != 4 || bytes.Contains(transcript.Bytes(), []byte(`"coding_contract_version":1`)) {
		t.Fatal("Rust emitted a non-native tool transcript")
	}
	replayed, err := codingrunner.ReplayHostedFrozenSubmission(t.Context(), codingrunner.HostedReplayAuthority{HostedAuthority: authority, FrozenPatchSHA256: frozen.Submission.FrozenPatchSHA256}, *frozen.Submission, bytes.NewReader(fixture.visible), manifest.Limits)
	if err != nil {
		t.Fatal(err)
	}
	defer replayed.Close()
	if _, err := client.Run(t.Context(), run); err == nil {
		t.Fatal("finished Rust case ran again")
	}
}
