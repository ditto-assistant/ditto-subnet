package inference

import (
	"math"
	"net/http/httptest"
	"testing"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/postgres"
)

func pricedGrant() *postgres.InferenceGrant {
	return &postgres.InferenceGrant{
		LedgerPromptPricePerToken:      pgtype.Float8{Float64: 1e-6, Valid: true},
		LedgerCompletionPricePerToken:  pgtype.Float8{Float64: 2e-6, Valid: true},
		EmbeddingPriceMicrousdPerToken: 0.004,
		CostCeilingMode:                costCeilingModeOff,
	}
}

func TestLedgerCostUsesStampedPricesNotTheProviderBill(t *testing.T) {
	g := pricedGrant()
	// 12 prompt at 1 micro-USD + 5 completion at 2 micro-USD.
	for _, providerCost := range []int64{0, 22, 2100, 1 << 40} {
		if got := ledgerCostMicrousd(g, kindChat, 12, 5, providerCost); got != 22 {
			t.Fatalf("provider cost %d moved the ledger: %d", providerCost, got)
		}
	}
}

func TestLedgerCostUnpricedChatFallsBackToTrustedProviderBill(t *testing.T) {
	g := pricedGrant()
	g.LedgerPromptPricePerToken = pgtype.Float8{}
	if got := ledgerCostMicrousd(g, kindChat, 12, 5, 2100); got != 2100 {
		t.Fatalf("unpriced fallback: %d", got)
	}
	if got := ledgerCostMicrousd(g, kindChat, 12, 5, -7); got != 0 {
		t.Fatalf("negative provider cost must clamp: %d", got)
	}
	g.LedgerPromptPricePerToken = pgtype.Float8{Float64: math.NaN(), Valid: true}
	if got := ledgerCostMicrousd(g, kindChat, 12, 5, 9); got != 9 {
		t.Fatalf("a non-finite stamp is no price: %d", got)
	}
}

func TestLedgerCostClampsMissingAndHugeUsage(t *testing.T) {
	g := pricedGrant()
	if got := ledgerCostMicrousd(g, kindChat, 0, 0, 0); got != 0 {
		t.Fatalf("no usage: %d", got)
	}
	if got := ledgerCostMicrousd(g, kindChat, -50, -1, 0); got != 0 {
		t.Fatalf("negative usage: %d", got)
	}
	g.LedgerPromptPricePerToken = pgtype.Float8{Float64: 1e12, Valid: true}
	if got := ledgerCostMicrousd(g, kindChat, math.MaxInt64, 0, 0); got != maxLedgerCostMicrousd {
		t.Fatalf("overflow must clamp to %d, got %d", maxLedgerCostMicrousd, got)
	}
}

func TestEmbeddingDefaultStampIsTheLegacyCatalogPrice(t *testing.T) {
	g := pricedGrant()
	for tokens := int64(0); tokens < 5000; tokens++ {
		legacy := int64(math.RoundToEven(float64(tokens) * 0.004))
		if got := embeddingCatalogCostMicrousd(g, tokens); got != legacy {
			t.Fatalf("tokens=%d: stamped cost %d != legacy %d", tokens, got, legacy)
		}
		if got := ledgerCostMicrousd(g, kindEmbedding, tokens, 0, 999); got != legacy {
			t.Fatalf("tokens=%d: embedding ledger %d != legacy %d", tokens, got, legacy)
		}
	}
	g.EmbeddingPriceMicrousdPerToken = 2
	if got := ledgerCostMicrousd(g, kindEmbedding, 5, 0, 0); got != 10 {
		t.Fatalf("stamped embedding price: %d", got)
	}
}

func TestCostCeilingPredicates(t *testing.T) {
	g := pricedGrant()
	g.LedgerCostMicrousd = 100
	if costCeilingActive(g) || costCeilingReached(g) {
		t.Fatal("no ceiling stamped must be inert")
	}
	g.CostCeilingMicrousd = pgtype.Int8{Int64: 100, Valid: true}
	if costCeilingActive(g) {
		t.Fatal("off mode with a ceiling must be inert")
	}
	g.CostCeilingMode = costCeilingModeShadow
	if !costCeilingReached(g) || costCeilingEnforcedAndReached(g) {
		t.Fatal("shadow: reached but never enforced")
	}
	g.CostCeilingMode = costCeilingModeEnforce
	if !costCeilingEnforcedAndReached(g) {
		t.Fatal("enforce at the ceiling must refuse")
	}
	g.LedgerCostMicrousd = 99
	if costCeilingReached(g) {
		t.Fatal("below the ceiling must admit")
	}
}

// A future lane joins the ceiling purely by booking ledger cost: the gate
// reads only the lane-agnostic total, never a lane's units.
func TestSyntheticThirdLaneContributesOnlyThroughLedger(t *testing.T) {
	g := pricedGrant()
	g.CostCeilingMicrousd = pgtype.Int8{Int64: 1000, Valid: true}
	g.CostCeilingMode = costCeilingModeEnforce
	// Chat and embeddings book some ledger cost; the third lane books the rest.
	g.LedgerCostMicrousd += ledgerCostMicrousd(g, kindChat, 12, 5, 0)       // 22
	g.LedgerCostMicrousd += ledgerCostMicrousd(g, kindEmbedding, 500, 0, 0) // 2
	if costCeilingReached(g) {
		t.Fatal("chat+embedding alone are below the ceiling")
	}
	const decisionLaneLedger = 976 // what a "decision" lane's settle would add
	g.LedgerCostMicrousd += decisionLaneLedger
	if g.PromptTokens+g.CompletionTokens+g.EmbeddingTokens != 0 {
		t.Fatal("this test books no unit counters")
	}
	if !costCeilingEnforcedAndReached(g) {
		t.Fatalf("ledger %d must meet the ceiling", g.LedgerCostMicrousd)
	}
}

func TestLedgerHeaderOnlyWithLiveCeiling(t *testing.T) {
	g := pricedGrant()
	g.LedgerCostMicrousd = 42
	w := httptest.NewRecorder()
	setLedgerCostHeader(w, g)
	setLedgerCostHeader(w, nil)
	if _, present := w.Header()[headerLedgerCostMicrousd]; present {
		t.Fatal("off mode must send no ledger header")
	}
	g.CostCeilingMicrousd = pgtype.Int8{Int64: 100, Valid: true}
	g.CostCeilingMode = costCeilingModeShadow
	setLedgerCostHeader(w, g)
	if got := w.Header().Get(headerLedgerCostMicrousd); got != "42" {
		t.Fatalf("ledger header: %q", got)
	}
}

func TestDecisionLaneLedgerMatchesTheDecisionMeter(t *testing.T) {
	g := pricedGrant()
	g.DecisionInputMicrousdPerMtok = 42_000 // $0.042 per million input tokens
	for _, tc := range []struct{ tokens, want int64 }{
		{0, 0},
		{1_000_000, 42_000},
		{25, 1},  // 1.05 -> 1
		{125, 5}, // 5.25 -> 5
		{1_000_000_000, 42_000_000},
	} {
		if got := ledgerCostMicrousd(g, kindDecision, tc.tokens, 999, 7777); got != tc.want {
			t.Fatalf("tokens=%d: got %d want %d", tc.tokens, got, tc.want)
		}
	}
	// Half-to-even, as decisionLedgerCost books decision_cost_microusd:
	// 0.5 -> 0, 1.5 -> 2.
	g.DecisionInputMicrousdPerMtok = 500_000
	if got := ledgerCostMicrousd(g, kindDecision, 1, 0, 0); got != 0 {
		t.Fatalf("0.5 must round to even 0, got %d", got)
	}
	if got := ledgerCostMicrousd(g, kindDecision, 3, 0, 0); got != 2 {
		t.Fatalf("1.5 must round to even 2, got %d", got)
	}
	for _, tokens := range []int64{1, 3, 25, 125, 1_000_000} {
		if got, want := ledgerCostMicrousd(g, kindDecision, tokens, 0, 0), decisionLedgerCost(tokens, 500_000); got != want {
			t.Fatalf("tokens=%d: ledger %d differs from the decision meter %d", tokens, got, want)
		}
	}
	g.DecisionInputMicrousdPerMtok = math.MaxInt64
	if got := ledgerCostMicrousd(g, kindDecision, math.MaxInt64, 0, 0); got != maxLedgerCostMicrousd {
		t.Fatalf("overflow must clamp, got %d", got)
	}
}
