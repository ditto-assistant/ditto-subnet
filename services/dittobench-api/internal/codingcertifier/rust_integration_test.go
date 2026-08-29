package codingcertifier

import (
	"net/http"
	"os"
	"testing"
	"time"
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
