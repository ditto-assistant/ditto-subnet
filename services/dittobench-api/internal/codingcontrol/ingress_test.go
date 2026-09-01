package codingcontrol

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/google/uuid"
	"github.com/mr-tron/base58"
	"golang.org/x/crypto/blake2b"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
)

const testToken = "executor-control-local-token-00000000000000000001"

type signer struct {
	secret *schnorrkel.SecretKey
	hotkey string
}

func newSigner(t *testing.T) signer {
	t.Helper()
	secret, public, err := schnorrkel.GenerateKeypair()
	if err != nil {
		t.Fatal(err)
	}
	encodedPublic := public.Encode()
	raw := append([]byte{42}, encodedPublic[:]...)
	hasher, err := blake2b.New512(nil)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = hasher.Write([]byte("SS58PRE"))
	_, _ = hasher.Write(raw)
	checksum := hasher.Sum(nil)
	raw = append(raw, checksum[:2]...)
	return signer{secret: secret, hotkey: base58.Encode(raw)}
}

func (value signer) header(
	t *testing.T,
	body []byte,
	operation string,
	nonce string,
) string {
	t.Helper()
	now := time.Date(2026, 9, 1, 0, 0, 30, 0, time.UTC)
	digest := sha256.Sum256(body)
	envelope := codingcontract.ExecutorControlEnvelope{
		Schema: codingcontract.ExecutorControlSchema, CodingContractVersion: 1,
		WeightEligible: false, ValidatorHotkey: value.hotkey,
		AgentID: "10000000-0000-4000-8000-000000000001", AgentArtifactSHA256: strings.Repeat("1", 64),
		CodingRunID: "coding-run-001", TicketID: "20000000-0000-4000-8000-000000000002",
		Operation: operation, Method: http.MethodPost, RequestBodySHA256: hex.EncodeToString(digest[:]),
		Nonce: nonce, IssuedAt: now.Add(-30 * time.Second), ExpiresAt: now.Add(30 * time.Second),
		Signature: strings.Repeat("0", 128),
	}
	message, err := codingcontract.ExecutorControlSigningMessage(envelope)
	if err != nil {
		t.Fatal(err)
	}
	signature, err := value.secret.Sign(
		schnorrkel.NewSigningContext([]byte("substrate"), message),
	)
	if err != nil {
		t.Fatal(err)
	}
	encodedSignature := signature.Encode()
	envelope.Signature = hex.EncodeToString(encodedSignature[:])
	raw, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	return base64.RawURLEncoding.EncodeToString(raw)
}

func newIngress(t *testing.T, allowed signer, downstream http.Handler) *Ingress {
	t.Helper()
	value, err := New(Config{
		Downstream: downstream, ControlToken: testToken, ValidatorHotkey: allowed.hotkey,
		Now:             func() time.Time { return time.Date(2026, 9, 1, 0, 0, 30, 0, time.UTC) },
		VerifySignature: codingcontract.VerifyExecutorSR25519, MaximumNonces: 8,
	})
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func TestIngressVerifiesEnvelopeBindsBodyAndInjectsOnlyLocalToken(t *testing.T) {
	allowed := newSigner(t)
	body := []byte(`{"ticket_id":"20000000-0000-4000-8000-000000000002","coding_run_id":"coding-run-001"}`)
	downstream := http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer "+testToken ||
			request.Header.Get(EnvelopeHeader) != "" {
			t.Fatal("private token or external envelope handoff is invalid")
		}
		observed, err := io.ReadAll(request.Body)
		if err != nil || string(observed) != string(body) {
			t.Fatalf("body=%q err=%v", observed, err)
		}
		response.WriteHeader(http.StatusNoContent)
	})
	ingress := newIngress(t, allowed, downstream)
	request := httptest.NewRequest(http.MethodPost, "/v1/coding/supervisor/author", strings.NewReader(string(body)))
	request.Header.Set(EnvelopeHeader, allowed.header(t, body, "supervisor.author", "30000000-0000-4000-8000-000000000003"))
	response := httptest.NewRecorder()
	ingress.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusNoContent {
		t.Fatalf("status=%d body=%q", response.Code, response.Body.String())
	}

	replay := httptest.NewRequest(http.MethodPost, request.URL.Path, strings.NewReader(string(body)))
	replay.Header.Set(EnvelopeHeader, request.Header.Get(EnvelopeHeader))
	replayResponse := httptest.NewRecorder()
	ingress.Handler().ServeHTTP(replayResponse, replay)
	if replayResponse.Code != http.StatusUnauthorized {
		t.Fatalf("replay status=%d", replayResponse.Code)
	}
}

func TestIngressRejectsAuthorityDriftBeforeDownstream(t *testing.T) {
	allowed := newSigner(t)
	other := newSigner(t)
	called := false
	ingress := newIngress(t, allowed, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	body := []byte(`{"ticket_id":"20000000-0000-4000-8000-000000000002","coding_run_id":"coding-run-001"}`)
	tests := map[string]struct {
		path   string
		body   []byte
		header string
	}{
		"missing envelope": {"/v1/coding/supervisor/author", body, ""},
		"wrong signer":     {"/v1/coding/supervisor/author", body, other.header(t, body, "supervisor.author", uuid.NewString())},
		"operation drift":  {"/v1/coding/supervisor/grade", body, allowed.header(t, body, "supervisor.author", uuid.NewString())},
		"body drift":       {"/v1/coding/supervisor/author", append(append([]byte(nil), body...), ' '), allowed.header(t, body, "supervisor.author", uuid.NewString())},
		"ticket drift":     {"/v1/coding/supervisor/author", []byte(`{"ticket_id":"40000000-0000-4000-8000-000000000004","coding_run_id":"coding-run-001"}`), allowed.header(t, []byte(`{"ticket_id":"40000000-0000-4000-8000-000000000004","coding_run_id":"coding-run-001"}`), "supervisor.author", uuid.NewString())},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, test.path, strings.NewReader(string(test.body)))
			request.Header.Set(EnvelopeHeader, test.header)
			response := httptest.NewRecorder()
			ingress.Handler().ServeHTTP(response, request)
			if response.Code != http.StatusUnauthorized || response.Body.String() != `{"error":"executor_control_rejected"}`+"\n" {
				t.Fatalf("status=%d body=%q", response.Code, response.Body.String())
			}
		})
	}
	if called {
		t.Fatal("rejected ingress reached the private handler")
	}
}

func TestIngressConfigurationAndDiagnosticsArePrivate(t *testing.T) {
	config := Config{ControlToken: testToken}
	if !strings.Contains(fmt.Sprintf("%#v", config), "private") ||
		strings.Contains(fmt.Sprintf("%#v", config), testToken) {
		t.Fatal("ingress configuration exposed its local token")
	}
	if _, err := json.Marshal(config); !errors.Is(err, ErrPrivate) {
		t.Fatalf("marshal error=%v", err)
	}
	if _, err := New(config); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("invalid config error=%v", err)
	}
}
