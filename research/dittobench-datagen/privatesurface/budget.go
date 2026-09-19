package privatesurface

import (
	"errors"
	"math"
	"sync"
)

// BudgetSnapshot contains billing metadata only. Reservations remain charged
// on transport failures, missing usage, and crashes; those are not free calls.
type BudgetSnapshot struct {
	LimitUSD    float64 `json:"limit_usd"`
	ChargedUSD  float64 `json:"charged_or_reserved_usd"`
	ReportedUSD float64 `json:"reported_usd"`
	Requests    int     `json:"requests"`
	Blocked     bool    `json:"blocked"`
}

type spendBudget struct {
	mu            sync.Mutex
	state         BudgetSnapshot
	checkpoint    func(BudgetSnapshot) error
	outputReserve int
}

const priceCeilingPerMillion = 20.0

// EnableBudget must be called before starting requests. It is a per-invocation
// guard, not an account-wide quota: operators must allocate this limit from
// their remaining total and retain each invocation's checkpoint.
//
// The initial bound is restricted to the reviewed text-only qualification
// routes. We reserve 65,536 output tokens (including hidden reasoning), well
// above the requested 4,096, plus one input token per serialized byte and
// 4,096 framing tokens. Price ceilings are also sent to OpenRouter. Unknown
// charges keep the complete reservation. Adding another route needs a fresh
// token/billing bound; don't silently generalize this to arbitrary models.
func (c *Client) EnableBudget(limit float64, checkpoint func(BudgetSnapshot) error) error {
	if limit <= 0 || limit > 1000 || math.IsNaN(limit) || math.IsInf(limit, 0) || checkpoint == nil || c.budget != nil {
		return errors.New("private producer: invalid spending budget")
	}
	routes := map[string]string{"openai/gpt-4.1": "azure", "google/gemini-2.5-flash": "google-vertex", "openai/gpt-5.4-mini": "azure"}
	if routes[c.profile.RewriteModel] != c.profile.RewriteProvider || routes[c.profile.ValidatorModel] != c.profile.ValidatorProvider {
		return errors.New("private producer: spending bound unavailable for route")
	}
	b := &spendBudget{state: BudgetSnapshot{LimitUSD: limit}, checkpoint: checkpoint}
	// Reviewed OpenRouter endpoint 2026-09-18: GPT-5.4-mini/Azure advertises
	// 128,000 max completion tokens, including reasoning. Reserve that full
	// maximum at the $20/M ceiling, even though requests ask for only 4,096.
	if c.profile.RewriteModel == "openai/gpt-5.4-mini" || c.profile.ValidatorModel == "openai/gpt-5.4-mini" {
		b.outputReserve = 128000
	}
	if err := checkpoint(b.state); err != nil {
		return errors.New("private producer: budget checkpoint failed")
	}
	c.budget = b
	return nil
}

func (b *spendBudget) reserve(bytes int) (float64, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	outputReserve := b.outputReserve
	if outputReserve == 0 {
		outputReserve = 65536
	}
	reserve := math.Ceil(float64(bytes+4096+outputReserve)*priceCeilingPerMillion) / 1e6
	if b.state.Blocked || reserve > b.state.LimitUSD-b.state.ChargedUSD {
		return 0, errors.New("private producer: spending budget exhausted")
	}
	b.state.ChargedUSD += reserve
	b.state.Requests++
	// Persist BEFORE sending. A process crash must not erase the reservation.
	if err := b.checkpoint(b.state); err != nil {
		b.state.Blocked = true
		return 0, errors.New("private producer: budget checkpoint failed")
	}
	return reserve, nil
}

func (b *spendBudget) settle(reserved float64, cost *float64) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if cost == nil || math.IsNaN(*cost) || math.IsInf(*cost, 0) || *cost < 0 || *cost > reserved {
		if cost != nil && !math.IsNaN(*cost) && !math.IsInf(*cost, 0) && *cost > reserved {
			b.state.ChargedUSD += *cost - reserved
			b.state.ReportedUSD += *cost
		}
		b.state.Blocked = true
		_ = b.checkpoint(b.state)
		return errors.New("private producer: billing receipt outside spending bound")
	}
	// Round charges up, never refund sub-microdollar precision into the budget.
	charged := math.Ceil(*cost*1e6) / 1e6
	b.state.ChargedUSD += charged - reserved
	b.state.ReportedUSD += charged
	if err := b.checkpoint(b.state); err != nil {
		b.state.Blocked = true
		return errors.New("private producer: budget checkpoint failed")
	}
	return nil
}
