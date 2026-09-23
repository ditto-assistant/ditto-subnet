package main

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

func newPrivateVerifierLedgerFixture(t *testing.T) (*inferenceBroker, privateVerifierCaseIdentity, *brokerSession) {
	t.Helper()
	identity := privateVerifierCaseIdentity{
		SessionID: uuid.NewString(), AgentID: uuid.NewString(),
		AttemptID: uuid.NewString(), ArtifactSHA256: strings.Repeat("a", 64),
		ImageSHA256: strings.Repeat("b", 64), CaseID: "hidden-case-do-not-export",
	}
	session := &brokerSession{
		id: identity.SessionID, ticketAgentID: identity.AgentID,
		benchVersion:             protocol.BenchVersionV13,
		sourceCapabilityRequired: true, sourceCapabilityActive: true,
	}
	broker := &inferenceBroker{sessions: map[string]*brokerSession{identity.SessionID: session}}
	return broker, identity, session
}

func settlePrivateVerifierFixture(session *brokerSession) {
	session.mu.Lock()
	session.sourceCapabilityActive = false
	session.mu.Unlock()
}

func TestPrivateVerifierLedgerZeroCallAndIdentity(t *testing.T) {
	broker, identity, session := newPrivateVerifierLedgerFixture(t)
	if err := broker.bindPrivateVerifierCase(identity); err != nil {
		t.Fatal(err)
	}
	if err := broker.bindPrivateVerifierCase(identity); err == nil {
		t.Fatal("second binding accepted")
	}
	if _, ok := broker.beginRunCase(identity.SessionID, identity.CaseID); !ok {
		t.Fatal("bound case was not registered")
	}
	if _, err := broker.settledPrivateVerifierCaseLedger(identity); err == nil {
		t.Fatal("unsettled case exported")
	}
	broker.endRunCase(identity.SessionID, identity.CaseID)
	if _, err := broker.settledPrivateVerifierCaseLedger(identity); err == nil {
		t.Fatal("active source exported")
	}
	settlePrivateVerifierFixture(session)
	ledger, err := broker.settledPrivateVerifierCaseLedger(identity)
	if err != nil || ledger.Status != "zero_call" || ledger.AttributedResponses != 0 {
		t.Fatalf("ledger = %+v, err = %v", ledger, err)
	}
	encoded, err := json.Marshal(ledger)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), identity.CaseID) || strings.Contains(string(encoded), identity.SessionID) {
		t.Fatal("hidden case or session identity leaked")
	}
	wrong := identity
	wrong.ImageSHA256 = strings.Repeat("c", 64)
	if _, err := broker.settledPrivateVerifierCaseLedger(wrong); err == nil {
		t.Fatal("wrong image identity exported")
	}
}

func TestPrivateVerifierLedgerRejectsDirtyOrWrongSession(t *testing.T) {
	broker, identity, session := newPrivateVerifierLedgerFixture(t)
	wrong := identity
	wrong.AttemptID = uuid.NewString()
	wrong.AgentID = uuid.NewString()
	if err := broker.bindPrivateVerifierCase(wrong); err == nil {
		t.Fatal("wrong agent bound")
	}
	session.chatDispatches = 1
	if err := broker.bindPrivateVerifierCase(identity); err == nil {
		t.Fatal("dirty session bound")
	}
	session.chatDispatches = 0
	identity.ArtifactSHA256 = "not-a-digest"
	if err := broker.bindPrivateVerifierCase(identity); err == nil {
		t.Fatal("invalid artifact digest bound")
	}
}

func TestPrivateVerifierLedgerClassifiesAttributionFailures(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*inferenceBroker, privateVerifierCaseIdentity, *brokerSession)
		want   string
	}{
		{"attributed", func(_ *inferenceBroker, id privateVerifierCaseIdentity, s *brokerSession) {
			s.chatDispatches = 1
			recordClaimSpanCompletionLocked(s, claimSpanAttribution{enabled: true, exact: true, caseID: id.CaseID},
				[]byte(`{"messages":[{"role":"user","content":"request"}]}`),
				[]byte(`{"choices":[{"message":{"role":"assistant","content":"response"}}]}`))
		}, "attributed"},
		{"unattributed", func(_ *inferenceBroker, _ privateVerifierCaseIdentity, s *brokerSession) {
			s.chatDispatches = 1
			recordClaimSpanCompletionLocked(s, claimSpanAttribution{enabled: true},
				[]byte(`{}`), []byte(`{}`))
		}, "unattributed"},
		{"truncated", func(_ *inferenceBroker, id privateVerifierCaseIdentity, s *brokerSession) {
			s.claimSpanCases[id.CaseID].ledger.Truncated = true
		}, "truncated"},
		{"unreadable request", func(_ *inferenceBroker, id privateVerifierCaseIdentity, s *brokerSession) {
			s.claimSpanCases[id.CaseID].unparseableRequests = 1
		}, "unreadable"},
		{"cross-case claim", func(_ *inferenceBroker, _ privateVerifierCaseIdentity, s *brokerSession) {
			_ = beginClaimSpanCompletionLocked(s, 0, "different-hidden-case")
		}, "cross_case"},
		{"cross-case start", func(b *inferenceBroker, id privateVerifierCaseIdentity, _ *brokerSession) {
			if _, ok := b.beginRunCase(id.SessionID, "different-hidden-case"); ok {
				t.Fatal("cross-case start accepted")
			}
		}, "cross_case"},
		{"failed dispatch", func(_ *inferenceBroker, _ privateVerifierCaseIdentity, s *brokerSession) {
			s.chatDispatches = 1
		}, "no_successful_response"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			broker, identity, session := newPrivateVerifierLedgerFixture(t)
			if err := broker.bindPrivateVerifierCase(identity); err != nil {
				t.Fatal(err)
			}
			if _, ok := broker.beginRunCase(identity.SessionID, identity.CaseID); !ok {
				t.Fatal("bound case not registered")
			}
			tc.mutate(broker, identity, session)
			broker.endRunCase(identity.SessionID, identity.CaseID)
			settlePrivateVerifierFixture(session)
			ledger, err := broker.settledPrivateVerifierCaseLedger(identity)
			if err != nil || ledger.Status != tc.want {
				t.Fatalf("ledger = %+v, err = %v; want %s", ledger, err, tc.want)
			}
		})
	}
}
