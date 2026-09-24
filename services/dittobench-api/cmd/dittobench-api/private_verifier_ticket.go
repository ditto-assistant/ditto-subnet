package main

// Private verifier admission is dormant until a scorer-owned sandbox factory
// proves the exact screened image and stop/drain lifecycle. The Platform MAC
// is required independently of the ordinary control plane, whose rollout may
// still be in shadow mode. These routes never produce a policy verdict.

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/google/uuid"
)

const privateCaseTicketDomain = "ditto-v13-private-case-ticket-v1\x00"

type privateCaseTicket struct {
	Body      string `json:"body"`
	MACSHA256 string `json:"mac_sha256"`
}

type privateCaseClaims struct {
	Revision       string    `json:"revision"`
	GroupID        string    `json:"group_id"`
	Role           string    `json:"role"`
	AgentID        string    `json:"agent_id"`
	AttemptID      string    `json:"attempt_id"`
	ArtifactSHA256 string    `json:"artifact_sha256"`
	ImageSHA256    string    `json:"image_sha256"`
	ImageUploadID  string    `json:"image_upload_id"`
	ProfileSHA256  string    `json:"profile_sha256"`
	ManifestSHA256 string    `json:"manifest_sha256"`
	SessionID      string    `json:"session_id"`
	CaseID         string    `json:"case_id"`
	Nonce          string    `json:"nonce"`
	IssuedAt       time.Time `json:"issued_at"`
	ExpiresAt      time.Time `json:"expires_at"`
}

func decodePrivateCaseTicket(ticket privateCaseTicket, key []byte, now time.Time) (privateCaseClaims, error) {
	var claims privateCaseClaims
	if len(key) < 32 || len(ticket.Body) == 0 || len(ticket.Body) > 4096 || len(ticket.MACSHA256) != 64 {
		return claims, errors.New("private verifier ticket unavailable")
	}
	raw, err := base64.RawURLEncoding.DecodeString(ticket.Body)
	if err != nil || len(raw) == 0 || len(raw) > 3072 {
		return claims, errors.New("private verifier ticket unavailable")
	}
	actual, err := hex.DecodeString(ticket.MACSHA256)
	if err != nil || len(actual) != sha256.Size {
		return claims, errors.New("private verifier ticket unavailable")
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(privateCaseTicketDomain))
	_, _ = mac.Write(raw)
	if !hmac.Equal(actual, mac.Sum(nil)) {
		return claims, errors.New("private verifier ticket unavailable")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&claims) != nil || decoder.Decode(new(any)) != io.EOF {
		return privateCaseClaims{}, errors.New("private verifier ticket unavailable")
	}
	if claims.Revision != "v13-private-case-ticket-v1" ||
		(claims.Role != "target" && claims.Role != "known_benign") ||
		!canonicalSHA256(claims.ArtifactSHA256) || !canonicalSHA256(claims.ImageSHA256) ||
		!canonicalSHA256(claims.ProfileSHA256) || !canonicalSHA256(claims.ManifestSHA256) ||
		!canonicalSHA256(claims.Nonce) || claims.Nonce == string(bytes.Repeat([]byte{'0'}, 64)) ||
		claims.IssuedAt.IsZero() || claims.ExpiresAt.IsZero() ||
		claims.IssuedAt.After(now) || !claims.ExpiresAt.After(now) ||
		!claims.ExpiresAt.After(claims.IssuedAt) ||
		claims.ExpiresAt.Sub(claims.IssuedAt) > 5*time.Minute {
		return privateCaseClaims{}, errors.New("private verifier ticket unavailable")
	}
	for _, value := range []string{
		claims.GroupID, claims.AgentID, claims.AttemptID, claims.ImageUploadID,
		claims.SessionID, claims.CaseID,
	} {
		if _, err := uuid.Parse(value); err != nil {
			return privateCaseClaims{}, errors.New("private verifier ticket unavailable")
		}
	}
	return claims, nil
}

type privateCaseAdmission struct {
	broker *inferenceBroker
	key    []byte
	mu     sync.Mutex
	// Only the future scorer-owned sandbox factory may set these. No request
	// field or public route can assert that an image ran or a container stopped.
	verifiedImages map[string]string
	stoppedCases   map[string]bool
	usedTickets    map[string]privateCaseTicketUse
}

type privateCaseTicketUse struct {
	bodySHA256 [sha256.Size]byte
	expiresAt  time.Time
}

func (a *privateCaseAdmission) bindVerifiedImage(sessionID, imageSHA256 string) bool {
	if a == nil || !canonicalSHA256(imageSHA256) {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.verifiedImages == nil || a.verifiedImages[sessionID] != "" {
		return false
	}
	a.verifiedImages[sessionID] = imageSHA256
	return true
}

// markContainerStopped is only for the scorer-owned factory after a strict
// StopRetainingImage succeeds. The route has no caller-controlled equivalent.
func (a *privateCaseAdmission) markContainerStopped(sessionID string) bool {
	if a == nil {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.stoppedCases == nil || a.verifiedImages[sessionID] == "" || a.stoppedCases[sessionID] {
		return false
	}
	a.stoppedCases[sessionID] = true
	return true
}

func (a *privateCaseAdmission) decodeRequest(w http.ResponseWriter, r *http.Request) (privateCaseClaims, privateCaseTicket, bool) {
	if a == nil || a.broker == nil || len(a.key) < 32 {
		http.Error(w, "private verifier unavailable", http.StatusServiceUnavailable)
		return privateCaseClaims{}, privateCaseTicket{}, false
	}
	defer r.Body.Close()
	var ticket privateCaseTicket
	decoder := json.NewDecoder(io.LimitReader(r.Body, 6145))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&ticket) != nil || decoder.Decode(new(any)) != io.EOF {
		http.Error(w, "private verifier ticket invalid", http.StatusUnauthorized)
		return privateCaseClaims{}, privateCaseTicket{}, false
	}
	claims, err := decodePrivateCaseTicket(ticket, a.key, time.Now().UTC())
	if err != nil {
		http.Error(w, "private verifier ticket invalid", http.StatusUnauthorized)
		return privateCaseClaims{}, privateCaseTicket{}, false
	}
	return claims, ticket, true
}

func (a *privateCaseAdmission) admit(w http.ResponseWriter, r *http.Request) {
	claims, ticket, ok := a.decodeRequest(w, r)
	if !ok {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.usedTickets == nil {
		http.Error(w, "private verifier unavailable", http.StatusServiceUnavailable)
		return
	}
	for nonce, used := range a.usedTickets {
		if !used.expiresAt.After(time.Now().UTC()) {
			delete(a.usedTickets, nonce)
		}
	}
	if len(a.usedTickets) >= 4096 {
		http.Error(w, "private verifier capacity unavailable", http.StatusServiceUnavailable)
		return
	}
	if a.verifiedImages[claims.SessionID] != claims.ImageSHA256 {
		http.Error(w, "private verifier image unavailable", http.StatusConflict)
		return
	}
	if _, used := a.usedTickets[claims.Nonce]; used {
		http.Error(w, "private verifier ticket used", http.StatusConflict)
		return
	}
	identity := privateVerifierCaseIdentity{
		SessionID: claims.SessionID, AgentID: claims.AgentID,
		AttemptID: claims.AttemptID, ArtifactSHA256: claims.ArtifactSHA256,
		ImageSHA256: claims.ImageSHA256, CaseID: claims.CaseID,
	}
	if err := a.broker.bindPrivateVerifierCase(identity); err != nil {
		http.Error(w, "private verifier session unavailable", http.StatusConflict)
		return
	}
	a.usedTickets[claims.Nonce] = privateCaseTicketUse{
		bodySHA256: sha256.Sum256([]byte(ticket.Body)), expiresAt: claims.ExpiresAt,
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]string{"status": "admitted_report_only"})
}

func (a *privateCaseAdmission) ledger(w http.ResponseWriter, r *http.Request) {
	claims, ticket, ok := a.decodeRequest(w, r)
	if !ok {
		return
	}
	a.mu.Lock()
	use, admitted := a.usedTickets[claims.Nonce]
	stopped := a.stoppedCases[claims.SessionID]
	image := a.verifiedImages[claims.SessionID]
	a.mu.Unlock()
	if !admitted || use.bodySHA256 != sha256.Sum256([]byte(ticket.Body)) ||
		!stopped || image != claims.ImageSHA256 {
		http.Error(w, "private verifier session unavailable", http.StatusConflict)
		return
	}
	ledger, err := a.broker.settledPrivateVerifierCaseLedger(privateVerifierCaseIdentity{
		SessionID: claims.SessionID, AgentID: claims.AgentID,
		AttemptID: claims.AttemptID, ArtifactSHA256: claims.ArtifactSHA256,
		ImageSHA256: claims.ImageSHA256, CaseID: claims.CaseID,
	})
	if err != nil {
		http.Error(w, "private verifier case unsettled", http.StatusConflict)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, ledger)
}
