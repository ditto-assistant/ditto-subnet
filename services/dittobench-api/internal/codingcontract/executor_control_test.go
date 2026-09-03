package codingcontract

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

func controlEnvelope() ExecutorControlEnvelope {
	issued := time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)
	return ExecutorControlEnvelope{
		Schema: ExecutorControlSchema, CodingContractVersion: 1, WeightEligible: false,
		ValidatorHotkey: "5" + strings.Repeat("A", 47), AgentID: "10000000-0000-4000-8000-000000000001",
		AgentArtifactSHA256: strings.Repeat("1", 64), CodingRunID: "coding-run-001",
		TicketID: "20000000-0000-4000-8000-000000000002", Operation: "supervisor.author", Method: "POST",
		RequestBodySHA256: strings.Repeat("2", 64), Nonce: "30000000-0000-4000-8000-000000000003",
		IssuedAt: issued, ExpiresAt: issued.Add(time.Minute), Signature: strings.Repeat("ab", 64),
	}
}

func TestExecutorControlSharedVector(t *testing.T) {
	raw, err := os.ReadFile("../../../../packages/dittobench-coding-contract/testdata/coding_executor_control_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var vector struct {
		Envelope ExecutorControlEnvelope `json:"envelope"`
		Expected string                  `json:"expected_signing_message_hex"`
	}
	if err := json.Unmarshal(raw, &vector); err != nil {
		t.Fatal(err)
	}
	message, err := ExecutorControlSigningMessage(vector.Envelope)
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(message) != vector.Expected {
		t.Fatal("shared signing message drifted")
	}
}

func TestExecutorControlVerifierRejectsReplayAndBodyDrift(t *testing.T) {
	now := time.Date(2026, 9, 1, 0, 0, 30, 0, time.UTC)
	verifier, err := NewExecutorControlVerifier(func() time.Time { return now }, func(_ string, _ []byte, signature []byte) bool {
		return len(signature) == 64
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"operation":"author"}`)
	digest := sha256.Sum256(body)
	value := controlEnvelope()
	value.RequestBodySHA256 = hex.EncodeToString(digest[:])
	if err := verifier.Verify(value, body); err != nil {
		t.Fatal(err)
	}
	if err := verifier.Verify(value, body); err == nil {
		t.Fatal("nonce replay accepted")
	}
	other := controlEnvelope()
	other.Nonce = "40000000-0000-4000-8000-000000000004"
	other.RequestBodySHA256 = value.RequestBodySHA256
	if err := verifier.Verify(other, []byte(`{"operation":"grade"}`)); err == nil {
		t.Fatal("body drift accepted")
	}
}

func TestExecutorControlSigningMessageMatchesPython(t *testing.T) {
	message, err := ExecutorControlSigningMessage(controlEnvelope())
	if err != nil {
		t.Fatal(err)
	}
	want := "dittobench-coding-executor-control:v1\x00" + "5" + strings.Repeat("A", 47) +
		"\x0010000000-0000-4000-8000-000000000001\x00" + strings.Repeat("1", 64) +
		"\x00coding-run-001\x0020000000-0000-4000-8000-000000000002\x00supervisor.author\x00POST\x00" +
		strings.Repeat("2", 64) + "\x0030000000-0000-4000-8000-000000000003\x002026-09-01T00:00:00.000000+00:00\x002026-09-01T00:01:00.000000+00:00"
	if string(message) != want {
		t.Fatalf("message=%q want=%q", message, want)
	}
}

func TestExecutorControlRejectsUnknownOperationAndLongLifetime(t *testing.T) {
	value := controlEnvelope()
	value.Operation = "supervisor.shell"
	if value.Validate() == nil {
		t.Fatal("unknown operation accepted")
	}
	value = controlEnvelope()
	value.ExpiresAt = value.IssuedAt.Add(3 * time.Minute)
	if value.Validate() == nil {
		t.Fatal("long-lived authority accepted")
	}
}
