package main

// This file is an in-process, read-only broker evidence seam for a future
// trusted V13 private verifier. It neither starts private cases nor exports an
// HTTP route. The trusted caller must independently establish the committed
// attempt, artifact and Docker image identities before installing a binding.

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

type privateVerifierCaseBinding struct {
	attemptID       string
	artifactSHA256  string
	imageSHA256     string
	caseID          string
	started         bool
	ended           bool
	crossCaseStarts uint64
	crossCaseClaims uint64
}

type privateVerifierCaseIdentity struct {
	SessionID      string
	AgentID        string
	AttemptID      string
	ArtifactSHA256 string
	ImageSHA256    string
	CaseID         string
}

// privateVerifierCaseLedger deliberately contains only identities, counts and
// a conservative status. It is not signed, a policy verdict, or CLEAR evidence.
// The session ID is hashed and the case ID is HMAC-bound to the random session
// ID so an export cannot reveal or dictionary-match hidden case names.
type privateVerifierCaseLedger struct {
	SessionSHA256       string `json:"session_sha256"`
	CaseSHA256          string `json:"case_sha256"`
	AgentID             string `json:"agent_id"`
	AttemptID           string `json:"attempt_id"`
	ArtifactSHA256      string `json:"artifact_sha256"`
	ImageSHA256         string `json:"image_sha256"`
	Status              string `json:"status"`
	ChatDispatches      uint64 `json:"chat_dispatches"`
	SuccessfulResponses uint64 `json:"successful_responses"`
	AttributedResponses uint64 `json:"attributed_responses"`
	Unattributed        uint64 `json:"unattributed"`
	UnreadableRequests  int    `json:"unreadable_requests"`
	Truncated           bool   `json:"truncated"`
	CrossCaseStarts     uint64 `json:"cross_case_starts"`
	CrossCaseClaims     uint64 `json:"cross_case_claims"`
}

func privateVerifierDigest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// The protected case inventory may use guessable identifiers. Key its export
// digest with the broker's random, unexported session ID so a reader cannot
// compare candidate IDs against a plain SHA-256 dictionary.
func privateVerifierCaseDigest(sessionID, caseID string) string {
	mac := hmac.New(sha256.New, []byte(sessionID))
	_, _ = mac.Write([]byte(caseID))
	return hex.EncodeToString(mac.Sum(nil))
}

// bindPrivateVerifierCase is callable only inside the trusted scorer process.
// It accepts a pristine V13 session and exactly one case. The caller must
// obtain attempt/artifact/image values from trusted Platform and Docker state,
// never from the miner request. Ordinary scored sessions never call this.
func (b *inferenceBroker) bindPrivateVerifierCase(identity privateVerifierCaseIdentity) error {
	if identity.SessionID == "" || identity.CaseID == "" ||
		strings.TrimSpace(identity.CaseID) != identity.CaseID ||
		identity.AgentID == "" || identity.AttemptID == "" ||
		!canonicalSHA256(identity.ArtifactSHA256) || !canonicalSHA256(identity.ImageSHA256) {
		return errors.New("invalid private verifier identity")
	}
	if _, err := uuid.Parse(identity.AgentID); err != nil {
		return errors.New("invalid private verifier agent")
	}
	if _, err := uuid.Parse(identity.AttemptID); err != nil {
		return errors.New("invalid private verifier attempt")
	}
	b.mu.RLock()
	session := b.sessions[identity.SessionID]
	b.mu.RUnlock()
	if session == nil {
		return errors.New("private verifier session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.id != identity.SessionID || session.ticketAgentID != identity.AgentID ||
		session.benchVersion != protocol.BenchVersionV13 ||
		!session.sourceCapabilityRequired || !session.sourceCapabilityActive ||
		session.privateVerifier != nil || session.inFlight != 0 ||
		session.chatDispatches != 0 || session.claimSpanCompletions != 0 ||
		session.claimSpanUnattributed != 0 || len(session.runCases) != 0 ||
		len(session.claimSpanCases) != 0 || len(session.sourceActiveHandlers) != 0 {
		return errors.New("private verifier session is not pristine")
	}
	session.privateVerifier = &privateVerifierCaseBinding{
		attemptID: identity.AttemptID, artifactSHA256: identity.ArtifactSHA256,
		imageSHA256: identity.ImageSHA256, caseID: identity.CaseID,
	}
	return nil
}

// settledPrivateVerifierCaseLedger returns no result until the sole case has
// ended, the source capability has been revoked, and all broker handlers have
// drained. A future runner must also independently stop its Docker container.
func (b *inferenceBroker) settledPrivateVerifierCaseLedger(identity privateVerifierCaseIdentity) (privateVerifierCaseLedger, error) {
	b.mu.RLock()
	session := b.sessions[identity.SessionID]
	b.mu.RUnlock()
	if session == nil {
		return privateVerifierCaseLedger{}, errors.New("private verifier session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	binding := session.privateVerifier
	if binding == nil || session.id != identity.SessionID ||
		session.ticketAgentID != identity.AgentID || binding.attemptID != identity.AttemptID ||
		binding.artifactSHA256 != identity.ArtifactSHA256 ||
		binding.imageSHA256 != identity.ImageSHA256 || binding.caseID != identity.CaseID {
		return privateVerifierCaseLedger{}, errors.New("private verifier identity mismatch")
	}
	if !binding.started || !binding.ended || !session.sourceCapabilityRequired ||
		session.sourceCapabilityActive || session.inFlight != 0 ||
		len(session.runCases) != 0 || len(session.sourceActiveHandlers) != 0 {
		return privateVerifierCaseLedger{}, errors.New("private verifier case is not settled")
	}
	ledger := session.claimSpanCases[binding.caseID]
	if ledger == nil || ledger.ledger == nil {
		return privateVerifierCaseLedger{}, errors.New("private verifier case ledger unavailable")
	}
	result := privateVerifierCaseLedger{
		SessionSHA256: privateVerifierDigest(session.id),
		CaseSHA256:    privateVerifierCaseDigest(session.id, binding.caseID),
		AgentID:       identity.AgentID, AttemptID: binding.attemptID,
		ArtifactSHA256: binding.artifactSHA256, ImageSHA256: binding.imageSHA256,
		ChatDispatches:      session.chatDispatches,
		SuccessfulResponses: session.claimSpanCompletions,
		AttributedResponses: uint64(ledger.ledger.Completions),
		Unattributed:        session.claimSpanUnattributed,
		UnreadableRequests:  ledger.unparseableRequests,
		Truncated:           ledger.ledger.Truncated,
		CrossCaseStarts:     binding.crossCaseStarts,
		CrossCaseClaims:     binding.crossCaseClaims,
	}
	switch {
	case result.CrossCaseStarts != 0 || result.CrossCaseClaims != 0 ||
		len(session.claimSpanCases) != 1:
		result.Status = "cross_case"
	case result.Truncated:
		result.Status = "truncated"
	case result.UnreadableRequests != 0:
		result.Status = "unreadable"
	case result.Unattributed != 0 || ledger.unattributedOverlap != 0 ||
		result.AttributedResponses != result.SuccessfulResponses:
		result.Status = "unattributed"
	case result.ChatDispatches == 0 && result.SuccessfulResponses == 0:
		result.Status = "zero_call"
	case result.SuccessfulResponses == 0:
		result.Status = "no_successful_response"
	default:
		result.Status = "attributed"
	}
	return result, nil
}
