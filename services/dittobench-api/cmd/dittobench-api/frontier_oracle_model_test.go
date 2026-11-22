package main

import (
	"slices"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

// oracleRouteSession activates one broker session on (model, profile) and
// returns it with its ticket identity, ready for a run claim.
func oracleRouteSession(t *testing.T, model, profile string) (*inferenceBroker, string, brokerTicketIdentity) {
	t.Helper()
	broker := newInferenceBroker(1)
	broker.platformProxyURL = "https://platform.example" + platformInferenceAPIPath
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, broker.platformProxyURL, "openrouter", profile, model)
	id := prepared["session_id"]
	broker.mu.RLock()
	session := broker.sessions[id]
	broker.mu.RUnlock()
	session.mu.Lock()
	defer session.mu.Unlock()
	return broker, id, brokerTicketIdentity{
		GrantID: session.grantID, AgentID: session.ticketAgentID,
		SlotID: session.ticketSlotID, TicketDeadline: session.ticketDeadline,
	}
}

func TestOnlyAFrontierOracleRunBindsTheOracleModel(t *testing.T) {
	v := protocol.BenchVersionV18

	// A panel, scored or dueling run never answers on the oracle model.
	broker, id, identity := oracleRouteSession(t, llm.FrontierOracleModel, llm.FrontierOracleProfileRevision)
	if broker.claimRunForMode(id, uuid.NewString(), identity, v, false) {
		t.Fatal("a non-oracle run bound the frontier oracle model")
	}

	// An oracle run binds it, and the run's relay preflight accepts it.
	broker, id, identity = oracleRouteSession(t, llm.FrontierOracleModel, llm.FrontierOracleProfileRevision)
	if !broker.claimRunForMode(id, uuid.NewString(), identity, v, true) {
		t.Fatal("an oracle run refused the pinned oracle route")
	}
	snapshot, err := broker.snapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if !snapshot.FrontierOracleRun || snapshot.Model != llm.FrontierOracleModel {
		t.Fatalf("oracle session snapshot = %+v", snapshot)
	}
	if err := requireTokenAccounting(snapshot, v, "full"); err != nil {
		t.Fatalf("oracle route preflight: %v", err)
	}

	// The oracle model on any other route is refused.
	broker, id, identity = oracleRouteSession(t, llm.FrontierOracleModel, llm.V9AggregateProfileRevision)
	if broker.claimRunForMode(id, uuid.NewString(), identity, v, true) {
		t.Fatal("the oracle model bound on the bench contract's route")
	}

	// Below bench v9 there is no oracle route.
	broker, id, identity = oracleRouteSession(t, llm.FrontierOracleModel, llm.FrontierOracleProfileRevision)
	if broker.claimRunForMode(id, uuid.NewString(), identity, protocol.BenchVersionV8, true) {
		t.Fatal("the oracle route bound below bench v9")
	}

	// An oracle run on the contract's own model and route still binds, as
	// before the oracle model existed, and is not marked an oracle-route run.
	broker, id, identity = oracleRouteSession(t, llm.V7HarnessModel, llm.V9AggregateProfileRevision)
	if !broker.claimRunForMode(id, uuid.NewString(), identity, v, true) {
		t.Fatal("an oracle run refused the bench contract's route")
	}
	snapshot, err = broker.snapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.FrontierOracleRun {
		t.Fatal("a contract-route oracle run was marked an oracle-route run")
	}
	if err := requireTokenAccounting(snapshot, v, "full"); err != nil {
		t.Fatalf("contract route preflight: %v", err)
	}
}

func TestTokenAccountingNeverTrustsTheOracleRouteFromARelay(t *testing.T) {
	v := protocol.BenchVersionV18
	snapshot := relayHealthSnapshot{
		AccountingVersion: 2, Provider: "openrouter",
		ProfileRevision: llm.FrontierOracleProfileRevision, Model: llm.FrontierOracleModel,
	}
	// A relay snapshot naming the oracle model is still a contract run.
	if err := requireTokenAccounting(snapshot, v, "full"); err == nil {
		t.Fatal("a non-oracle session accepted the oracle model")
	}
	snapshot.FrontierOracleRun = true
	if err := requireTokenAccounting(snapshot, v, "full"); err != nil {
		t.Fatalf("oracle session: %v", err)
	}
	// An oracle session must stay on the oracle route.
	snapshot.Model = llm.V7HarnessModel
	if err := requireTokenAccounting(snapshot, v, "full"); err == nil {
		t.Fatal("an oracle session accepted another model")
	}
}

func TestScorerAdvertisesTheFrontierOracleModel(t *testing.T) {
	s := &server{allowGeneratorRounds: true}
	if !slices.Contains(s.datasetFeatures(), "generator-frontier-oracle-model:"+llm.FrontierOracleModel) {
		t.Fatalf("features = %v", s.datasetFeatures())
	}
	if slices.ContainsFunc((&server{}).datasetFeatures(), func(f string) bool {
		return f == generatorFrontierOracleModelFeature()
	}) {
		t.Fatal("a scorer without generator rounds advertised the oracle model")
	}
}
