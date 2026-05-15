package bittensor

import "time"

// DiscountOpts controls how AggregateWithDiscount applies the memorisation
// discount documented in ditto/bench/docs/anti_gaming.md.
//
// The defaults (when the zero value is used) match the published example:
//
//	GapThreshold = 0.10
//	GapCeiling   = 0.30
//	MaxDiscount  = 0.50
//
// A miner whose canary mean trails its public mean by less than
// GapThreshold is not discounted; the discount climbs linearly until the
// gap reaches GapCeiling and saturates at MaxDiscount thereafter.
type DiscountOpts struct {
	GapThreshold float64
	GapCeiling   float64
	MaxDiscount  float64
}

// DefaultDiscountOpts returns the published-default DiscountOpts.
//
// Validators MAY override these per subnet policy, but the defaults are
// what the public anti-gaming doc promises miners; changes that lower
// MaxDiscount or raise GapThreshold should be announced in advance.
func DefaultDiscountOpts() DiscountOpts {
	return DiscountOpts{GapThreshold: 0.10, GapCeiling: 0.30, MaxDiscount: 0.50}
}

// applyDefaults fills zero fields with the published defaults so callers
// can pass DiscountOpts{} when they just want documented behaviour.
func (o DiscountOpts) applyDefaults() DiscountOpts {
	d := DefaultDiscountOpts()
	if o.GapThreshold == 0 {
		o.GapThreshold = d.GapThreshold
	}
	if o.GapCeiling == 0 {
		o.GapCeiling = d.GapCeiling
	}
	if o.MaxDiscount == 0 {
		o.MaxDiscount = d.MaxDiscount
	}
	return o
}

// MinerAggregate is the per-miner roll-up emitted by AggregateWithDiscount.
//
// PublicMean and CanaryMean are arithmetic means over the miner's per-case
// scores in each bucket; cases with Visibility "private" feed neither
// (they are graded but kept out of the discount math so an unlucky
// private split cannot tank a miner whose canary is fine).
type MinerAggregate struct {
	Hotkey         string
	PublicMean     float64
	CanaryMean     float64
	PublicSamples  int
	CanarySamples  int
	PrivateMean    float64
	PrivateSamples int
	Discount       float64 // multiplicative factor, [1 - MaxDiscount, 1]
	Weight         float64 // PublicMean * Discount, pre-normalisation
}

// AggregateWithDiscount groups Scores by miner hotkey, computes
// per-miner public/canary/private means, applies MemorisationDiscount to
// each miner's public mean, and returns the resulting AggregateWeights
// alongside the per-miner detail.
//
// hotkeyByChallenge maps each Score.ChallengeID to the miner's hotkey;
// scores whose ChallengeID is absent from the map are skipped (with a
// note so the validator can audit the drop). The output Weights map is
// normalised so its values sum to 1.0 (or is empty if every miner had
// zero weight). mechanism is stamped onto AggregateWeights.Mechanism.
func AggregateWithDiscount(
	scores []Score,
	hotkeyByChallenge map[string]string,
	mechanism Mechanism,
	opts DiscountOpts,
) (AggregateWeights, []MinerAggregate) {
	opts = opts.applyDefaults()

	type bucket struct {
		publicSum, canarySum, privateSum       float64
		publicCount, canaryCount, privateCount int
	}
	buckets := map[string]*bucket{}
	for _, s := range scores {
		hk, ok := hotkeyByChallenge[s.ChallengeID]
		if !ok || hk == "" {
			continue
		}
		b, ok := buckets[hk]
		if !ok {
			b = &bucket{}
			buckets[hk] = b
		}
		switch s.Visibility {
		case "canary":
			b.canarySum += s.Score
			b.canaryCount++
		case "private":
			b.privateSum += s.Score
			b.privateCount++
		default:
			b.publicSum += s.Score
			b.publicCount++
		}
	}

	out := make([]MinerAggregate, 0, len(buckets))
	weights := make(map[string]float64, len(buckets))
	var total float64
	for hk, b := range buckets {
		ma := MinerAggregate{Hotkey: hk}
		if b.publicCount > 0 {
			ma.PublicMean = b.publicSum / float64(b.publicCount)
			ma.PublicSamples = b.publicCount
		}
		if b.canaryCount > 0 {
			ma.CanaryMean = b.canarySum / float64(b.canaryCount)
			ma.CanarySamples = b.canaryCount
		}
		if b.privateCount > 0 {
			ma.PrivateMean = b.privateSum / float64(b.privateCount)
			ma.PrivateSamples = b.privateCount
		}
		ma.Discount = MemorisationDiscount(
			ma.PublicMean,
			ma.CanaryMean,
			ma.CanarySamples,
			opts.GapThreshold,
			opts.GapCeiling,
			opts.MaxDiscount,
		)
		ma.Weight = ma.PublicMean * ma.Discount
		total += ma.Weight
		weights[hk] = ma.Weight
		out = append(out, ma)
	}

	if total > 0 {
		for hk, w := range weights {
			weights[hk] = w / total
		}
	}

	return AggregateWeights{
		SchemaVersion: SchemaVersion,
		Mechanism:     mechanism,
		Weights:       weights,
		WindowEnd:     time.Now().UTC(),
	}, out
}
