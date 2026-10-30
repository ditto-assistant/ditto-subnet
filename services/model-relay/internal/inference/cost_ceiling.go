package inference

import (
	"math"
	"net/http"
	"strconv"

	"github.com/ditto-assistant/model-relay/internal/postgres"
)

// Per-run cost ceiling (#3271). Mirrors ditto/db/queries/inference.py.
//
// A grant's ledger_cost_microusd is receipted units times the list prices
// stamped on the grant at mint, summed over every lane at settlement. It is
// never the provider bill (cost_microusd / embedding_cost_microusd stay that),
// so provider routing, aggregator cost reports and cache hits cannot change
// what a run is charged. A lane joins the ceiling purely by booking ledger
// cost at settlement; admission compares the lane-agnostic total.
const (
	costCeilingModeOff     = "off"
	costCeilingModeShadow  = "shadow"
	costCeilingModeEnforce = "enforce"

	// Headers. The exchange pair is a fallback overlay for the JSON body
	// fields; the ledger header rides every authenticated proxied response.
	headerCostCeilingMicrousd = "X-Ditto-Cost-Ceiling-Microusd"
	headerCostCeilingMode     = "X-Ditto-Cost-Ceiling-Mode"
	headerLedgerCostMicrousd  = "X-Ditto-Ledger-Cost-Microusd"

	// maxLedgerCostMicrousd clamps one request's ledger cost to the largest
	// integer a float64 carries exactly.
	maxLedgerCostMicrousd = int64(1) << 53
)

// costCeilingActive reports whether the grant carries a stamped ceiling that
// admission must consult. Off mode, or a null ceiling, is byte-identical to a
// grant minted before the ceiling existed.
func costCeilingActive(g *postgres.InferenceGrant) bool {
	return g != nil && g.CostCeilingMicrousd.Valid &&
		(g.CostCeilingMode == costCeilingModeShadow || g.CostCeilingMode == costCeilingModeEnforce)
}

// costCeilingReached: settled ledger cost is at or past an active ceiling.
func costCeilingReached(g *postgres.InferenceGrant) bool {
	return costCeilingActive(g) && g.LedgerCostMicrousd >= g.CostCeilingMicrousd.Int64
}

// costCeilingEnforcedAndReached is the condition that refuses both lanes.
func costCeilingEnforcedAndReached(g *postgres.InferenceGrant) bool {
	return g != nil && g.CostCeilingMode == costCeilingModeEnforce && costCeilingReached(g)
}

// setLedgerCostHeader stamps X-Ditto-Ledger-Cost-Microusd with the grant's
// settled total when (and only when) the grant has an active ceiling.
func setLedgerCostHeader(w http.ResponseWriter, g *postgres.InferenceGrant) {
	if !costCeilingActive(g) {
		return
	}
	w.Header().Set(headerLedgerCostMicrousd, strconv.FormatInt(g.LedgerCostMicrousd, 10))
}

func finitePrice(v float64, valid bool) (float64, bool) {
	if !valid || math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
		return 0, false
	}
	return v, true
}

func clampLedger(v float64) int64 {
	if math.IsNaN(v) || v <= 0 {
		return 0
	}
	r := math.RoundToEven(v)
	if r >= float64(maxLedgerCostMicrousd) {
		return maxLedgerCostMicrousd
	}
	return int64(r)
}

// ledgerCostMicrousd prices one request's SETTLED (already clamped) units at
// the grant-stamped list price. Chat uses the stamped ledger prompt and
// completion prices (USD per token, the route_* unit); a chat grant minted
// with no list price at all falls back to the trusted provider bill so the
// ceiling still binds. Embeddings use the stamped micro-USD price, which at
// the shipped 0.004 reproduces the catalog figure the relay always booked.
// Half-to-even rounding matches the embedding settle and Python's round().
func ledgerCostMicrousd(g *postgres.InferenceGrant, kind string, promptTokens, completionTokens, trustedProviderCost int64) int64 {
	if kind == kindDecision {
		// The decision lane's own pricer (decisionLedgerCost: input tokens x
		// micro-USD per million, half-to-even), clamped. Output is unbilled,
		// and the same figure is booked as decision_cost_microusd.
		if g.DecisionInputMicrousdPerMtok <= 0 {
			return 0
		}
		return clampLedger(float64(max64(0, promptTokens)) * float64(g.DecisionInputMicrousdPerMtok) / 1_000_000)
	}
	if kind == kindEmbedding {
		price, ok := finitePrice(g.EmbeddingPriceMicrousdPerToken, true)
		if !ok {
			return 0
		}
		return clampLedger(float64(max64(0, promptTokens)) * price)
	}
	promptPrice, promptOK := finitePrice(g.LedgerPromptPricePerToken.Float64, g.LedgerPromptPricePerToken.Valid)
	completionPrice, completionOK := finitePrice(g.LedgerCompletionPricePerToken.Float64, g.LedgerCompletionPricePerToken.Valid)
	if !promptOK || !completionOK {
		cost := max64(0, trustedProviderCost)
		if cost > maxLedgerCostMicrousd {
			return maxLedgerCostMicrousd
		}
		return cost
	}
	return clampLedger((float64(max64(0, promptTokens))*promptPrice +
		float64(max64(0, completionTokens))*completionPrice) * 1_000_000)
}

// embeddingCatalogCostMicrousd is the embedding lane's cost_microusd: the
// grant-stamped catalog price (replacing the hard-coded 0.004), banker's
// rounding, provider cost never trusted. With the default stamp the result is
// bit-for-bit what the relay booked before #3271.
func embeddingCatalogCostMicrousd(g *postgres.InferenceGrant, promptTokens int64) int64 {
	return int64(math.RoundToEven(float64(promptTokens) * g.EmbeddingPriceMicrousdPerToken))
}
