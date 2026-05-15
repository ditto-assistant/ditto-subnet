package bittensor

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"sort"
	"strings"
	"unicode"
)

// HiddenSet partitions a fixture corpus into the three on-chain visibility
// buckets used by the subnet.
//
//   - Public: the fixture as shipped in the repo. Anyone can train against
//     these cases. They mostly exist to make miner onboarding cheap and to
//     keep regression baselines reproducible.
//
//   - Private: a validator-controlled subset that never ships in the repo.
//     Validators load these from an encrypted manifest at run time. They are
//     reshuffled on every tempo so miners cannot memorise them across
//     scoring windows.
//
//   - Canary: paraphrased versions of public cases. They share the same
//     ground truth as their public counterpart but use deliberately reworded
//     prompts/queries. Validators compare a miner's public score against its
//     canary score to detect verbatim memorisation; a large drop is evidence
//     of overfitting to the public split.
type HiddenSet struct {
	Public  []string
	Private []string
	Canary  []string
}

// PartitionFixture deterministically splits caseIDs into public/private/canary
// buckets using a validator-controlled secret. The same (caseIDs, secret)
// pair always produces the same partition; rotating the secret rotates the
// hidden set.
//
// privateFrac and canaryFrac are fractions of the input set; the remainder
// stays public. Out-of-range values are clamped to [0, 0.5] each (sum capped
// so at least 20% remains public after rounding).
func PartitionFixture(caseIDs []string, secret string, privateFrac, canaryFrac float64) HiddenSet {
	if privateFrac < 0 {
		privateFrac = 0
	}
	if canaryFrac < 0 {
		canaryFrac = 0
	}
	if privateFrac > 0.5 {
		privateFrac = 0.5
	}
	if canaryFrac > 0.5 {
		canaryFrac = 0.5
	}
	if privateFrac+canaryFrac > 0.8 {
		// Shrink proportionally so at least 20% stays public after rounding.
		scale := 0.8 / (privateFrac + canaryFrac)
		privateFrac *= scale
		canaryFrac *= scale
	}

	type scored struct {
		id   string
		hash string
	}
	items := make([]scored, 0, len(caseIDs))
	for _, id := range caseIDs {
		h := sha256.Sum256([]byte(secret + "|" + id))
		items = append(items, scored{id: id, hash: hex.EncodeToString(h[:])})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].hash < items[j].hash })

	n := float64(len(items))
	privateCount := int(math.Round(n * privateFrac))
	canaryCount := int(math.Round(n * canaryFrac))
	if privateCount+canaryCount >= len(items) {
		// Always leave at least one public case so honest miners can
		// onboard against the open dataset.
		over := privateCount + canaryCount - (len(items) - 1)
		if canaryCount >= over {
			canaryCount -= over
		} else {
			over -= canaryCount
			canaryCount = 0
			privateCount -= over
		}
	}

	set := HiddenSet{}
	for i, it := range items {
		switch {
		case i < privateCount:
			set.Private = append(set.Private, it.id)
		case i < privateCount+canaryCount:
			set.Canary = append(set.Canary, it.id)
		default:
			set.Public = append(set.Public, it.id)
		}
	}
	sort.Strings(set.Public)
	sort.Strings(set.Private)
	sort.Strings(set.Canary)
	return set
}

// ParaphraseSeed produces a deterministic, per-case canary salt. Combined
// with a paraphrase generator on the validator side, this guarantees that two
// validators sharing the same secret produce the same canary prompts for the
// same case ID — without leaking the secret to miners.
func ParaphraseSeed(secret, caseID string) string {
	h := sha256.Sum256([]byte(secret + "|paraphrase|" + caseID))
	return hex.EncodeToString(h[:])
}

// MemorisationDiscount computes the multiplicative weight discount applied to
// a miner whose canary performance is substantially below their public
// performance. The discount kicks in once the gap exceeds gapThreshold (e.g.
// 0.10) and saturates to maxDiscount once the gap reaches gapCeiling.
//
// The returned value is in [1 - maxDiscount, 1]. Multiply a miner's
// aggregate weight by this to penalise suspected memorisation. Returns 1.0
// when the miner has no canary samples (validator should run more canaries
// before applying a discount).
func MemorisationDiscount(publicMean, canaryMean float64, canarySamples int, gapThreshold, gapCeiling, maxDiscount float64) float64 {
	if canarySamples == 0 {
		return 1.0
	}
	gap := publicMean - canaryMean
	if gap <= gapThreshold {
		return 1.0
	}
	if gapCeiling <= gapThreshold {
		gapCeiling = gapThreshold + 1e-6
	}
	frac := (gap - gapThreshold) / (gapCeiling - gapThreshold)
	if frac > 1 {
		frac = 1
	}
	return 1.0 - maxDiscount*frac
}

// DistractorBundleFor builds a deterministic list of distractor pair IDs to
// pad a retrieval challenge. Distractors are drawn from the candidate pool
// but never overlap with expectedPairIDs or forbiddenPairIDs of the case
// under test.
//
// The set of distractor IDs is hashed against (secret, caseID) so the same
// validator reproduces the same distractor set on replay, but two different
// validators with different secrets see different distractors.
func DistractorBundleFor(caseID string, expectedPairIDs, forbiddenPairIDs, candidates []string, secret string, n int) []string {
	if n <= 0 || len(candidates) == 0 {
		return nil
	}
	disallow := make(map[string]bool, len(expectedPairIDs)+len(forbiddenPairIDs))
	for _, id := range expectedPairIDs {
		disallow[id] = true
	}
	for _, id := range forbiddenPairIDs {
		disallow[id] = true
	}

	type scored struct {
		id   string
		hash string
	}
	pool := make([]scored, 0, len(candidates))
	for _, id := range candidates {
		if disallow[id] {
			continue
		}
		h := sha256.Sum256([]byte(secret + "|distractor|" + caseID + "|" + id))
		pool = append(pool, scored{id: id, hash: hex.EncodeToString(h[:])})
	}
	sort.Slice(pool, func(i, j int) bool { return pool[i].hash < pool[j].hash })
	if n > len(pool) {
		n = len(pool)
	}
	out := make([]string, n)
	for i := 0; i < n; i++ {
		out[i] = pool[i].id
	}
	return out
}

// NormalisePromptForCanaryCheck returns a lower-case, punctuation-stripped
// version of s so paraphrase generators can confirm they actually changed
// the wording of a prompt before sending it as a canary.
func NormalisePromptForCanaryCheck(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			b.WriteRune(unicode.ToLower(r))
		case unicode.IsSpace(r):
			b.WriteByte(' ')
		}
	}
	return strings.Join(strings.Fields(b.String()), " ")
}

// EnsureParaphraseChanged returns nil when paraphrased differs meaningfully
// from original (different normalised tokens). Used by validators to refuse
// to ship a canary that is essentially identical to its public twin.
func EnsureParaphraseChanged(original, paraphrased string) error {
	if NormalisePromptForCanaryCheck(original) == NormalisePromptForCanaryCheck(paraphrased) {
		return fmt.Errorf("paraphrase identical to original after normalisation; refusing to ship as canary")
	}
	return nil
}
