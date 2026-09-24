package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

func signedPrivateTicket(t *testing.T, identity privateVerifierCaseIdentity, key []byte) privateCaseTicket {
	t.Helper()
	now := time.Now().UTC().Truncate(time.Second)
	claims := privateCaseClaims{
		Revision: "v13-private-case-ticket-v1", GroupID: uuid.NewString(), Role: "target",
		AgentID: identity.AgentID, AttemptID: identity.AttemptID,
		ArtifactSHA256: identity.ArtifactSHA256, ImageSHA256: identity.ImageSHA256,
		ImageUploadID: uuid.NewString(), ProfileSHA256: strings.Repeat("c", 64),
		ManifestSHA256: strings.Repeat("d", 64), SessionID: identity.SessionID,
		CaseID: identity.CaseID, Nonce: strings.Repeat("e", 64),
		IssuedAt: now, ExpiresAt: now.Add(4 * time.Minute),
	}
	raw, err := json.Marshal(claims)
	if err != nil {
		t.Fatal(err)
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(privateCaseTicketDomain))
	_, _ = mac.Write(raw)
	return privateCaseTicket{Body: base64.RawURLEncoding.EncodeToString(raw), MACSHA256: hex.EncodeToString(mac.Sum(nil))}
}

func callPrivateTicketRoute(t *testing.T, admission *privateCaseAdmission, route string, ticket privateCaseTicket) *httptest.ResponseRecorder {
	t.Helper()
	body, err := json.Marshal(ticket)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, route, bytes.NewReader(body))
	response := httptest.NewRecorder()
	// Deliberately bypass ordinary control auth: these handlers must enforce
	// their own Platform-issued ticket even in shadow mode.
	if route == "/v1/private-verifier/admit" {
		admission.admit(response, request)
	} else {
		admission.ledger(response, request)
	}
	return response
}

func TestPrivateVerifierTicketAdmissionIsFailClosed(t *testing.T) {
	broker, identity, session := newPrivateVerifierLedgerFixture(t)
	identity.CaseID = uuid.NewString()
	key := []byte("synthetic-private-ticket-key-32-bytes")
	ticket := signedPrivateTicket(t, identity, key)
	admission := &privateCaseAdmission{
		broker: broker, key: key,
		verifiedImages: map[string]string{}, stoppedCases: map[string]bool{},
		usedTickets: map[string]privateCaseTicketUse{},
	}
	if got := callPrivateTicketRoute(t, admission, "/v1/private-verifier/admit", ticket).Code; got != http.StatusConflict {
		t.Fatalf("unverified image admitted: %d", got)
	}
	if !admission.bindVerifiedImage(identity.SessionID, identity.ImageSHA256) ||
		admission.bindVerifiedImage(identity.SessionID, identity.ImageSHA256) {
		t.Fatal("verified image binding was not single-use")
	}
	wrong := ticket
	wrong.MACSHA256 = strings.Repeat("0", 64)
	if got := callPrivateTicketRoute(t, admission, "/v1/private-verifier/admit", wrong).Code; got != http.StatusUnauthorized {
		t.Fatalf("bad MAC admitted: %d", got)
	}
	if got := callPrivateTicketRoute(t, admission, "/v1/private-verifier/admit", ticket).Code; got != http.StatusOK {
		t.Fatalf("valid exact ticket denied: %d", got)
	}
	if got := callPrivateTicketRoute(t, admission, "/v1/private-verifier/admit", ticket).Code; got != http.StatusConflict {
		t.Fatalf("ticket reused: %d", got)
	}
	if got := callPrivateTicketRoute(t, admission, "/v1/private-verifier/ledger", ticket).Code; got != http.StatusConflict {
		t.Fatalf("unsettled ledger exported: %d", got)
	}
	if _, started := broker.beginRunCase(identity.SessionID, identity.CaseID); !started {
		t.Fatal("private case not started")
	}
	broker.endRunCase(identity.SessionID, identity.CaseID)
	session.mu.Lock()
	session.sourceCapabilityActive = false
	session.mu.Unlock()
	if got := callPrivateTicketRoute(t, admission, "/v1/private-verifier/ledger", ticket).Code; got != http.StatusConflict {
		t.Fatalf("container not stopped, ledger exported: %d", got)
	}
	if !admission.markContainerStopped(identity.SessionID) {
		t.Fatal("scorer-owned stop marker rejected")
	}
	response := callPrivateTicketRoute(t, admission, "/v1/private-verifier/ledger", ticket)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "zero_call") {
		t.Fatalf("settled ledger unavailable: status=%d body=%s", response.Code, response.Body.String())
	}
	admission.key = nil
	if got := callPrivateTicketRoute(t, admission, "/v1/private-verifier/ledger", ticket).Code; got != http.StatusServiceUnavailable {
		t.Fatalf("missing verification key accepted: %d", got)
	}
}
