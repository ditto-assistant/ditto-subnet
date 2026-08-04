// Command gstudy is an offline reliability analyzer for DittoBench scored runs.
// It reads a JSONL of scored runs and reports:
//
//   - a crossed generalizability (G-study) variance decomposition of per-case
//     scores into seed, item (category), and residual components, so you can see
//     whether the benchmark is seed-dominated ("buy seeds") or item-dominated
//     ("buy items"). The dominant facet flips with benchmark size, and an
//     agent+memory bench is usually the small-N, seed-dominated regime;
//   - a lightweight 2PL-style per-category difficulty (mean pass) and
//     discrimination (spread) estimate, flagging saturated (everyone-passes) and
//     floor (everyone-fails) categories that carry ~0 Fisher information at the
//     champion boundary and should be retired or rebalanced.
//
// It is pure analysis over already-scored data: no LLM, no chain, stdlib only.
// Input JSONL, one run per line:
//
//	{"seed":123,"per_case":[{"category":"web_search","score":1.0}, ...]}
//
// Usage: gstudy < runs.jsonl   (or gstudy -in runs.jsonl)
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
)

type perCase struct {
	Category  string  `json:"category"`
	Score     float64 `json:"score"`
	LatencyMs float64 `json:"latency_ms,omitempty"` // optional; enables latency-flatness signal
}

type run struct {
	Seed    int64     `json:"seed"`
	Harness string    `json:"harness,omitempty"` // optional harness/miner id; groups the parser signal
	PerCase []perCase `json:"per_case"`
}

func main() {
	in := flag.String("in", "", "input JSONL path (default: stdin)")
	flag.Parse()

	r := os.Stdin
	if *in != "" {
		f, err := os.Open(*in)
		if err != nil {
			fmt.Fprintln(os.Stderr, "open:", err)
			os.Exit(1)
		}
		defer f.Close()
		r = f
	}

	runs, err := readRuns(r)
	if err != nil {
		fmt.Fprintln(os.Stderr, "read:", err)
		os.Exit(1)
	}
	if len(runs) == 0 {
		fmt.Fprintln(os.Stderr, "no runs read")
		os.Exit(1)
	}

	rep := analyze(runs)
	b, _ := json.MarshalIndent(rep, "", "  ")
	fmt.Println(string(b))
}

func readRuns(r *os.File) ([]run, error) {
	var out []run
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 1<<20), 64<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var rn run
		if err := json.Unmarshal(line, &rn); err != nil {
			return nil, err
		}
		out = append(out, rn)
	}
	return out, sc.Err()
}

// Report is the gstudy output.
type Report struct {
	Runs          int                   `json:"runs"`
	Cases         int                   `json:"cases"`
	GrandMean     float64               `json:"grand_mean"`
	Variance      VarianceComponents    `json:"variance_components"`
	DominantFacet string                `json:"dominant_facet"`
	Categories    []CategoryReliability `json:"categories"`
	ParserSignals ParserSignals         `json:"parser_signals"`
	Advice        string                `json:"advice"`
}

// VarianceComponents is the crossed decomposition of per-case score variance.
type VarianceComponents struct {
	Total     float64 `json:"total"`
	Seed      float64 `json:"seed"`     // between-seed (run) component
	Item      float64 `json:"item"`     // between-category (item) component
	Residual  float64 `json:"residual"` // seed×item interaction + noise
	SeedFrac  float64 `json:"seed_frac"`
	ItemFrac  float64 `json:"item_frac"`
	ResidFrac float64 `json:"residual_frac"`
}

// CategoryReliability is a 2PL-style per-category summary.
type CategoryReliability struct {
	Category       string  `json:"category"`
	N              int     `json:"n"`
	Difficulty     float64 `json:"difficulty"`     // mean pass (higher = easier)
	Discrimination float64 `json:"discrimination"` // std of scores (0 = no signal)
	Flag           string  `json:"flag,omitempty"` // "saturated" | "floor" | ""
}

const (
	saturatedAbove = 0.98
	floorBelow     = 0.02
)

func analyze(runs []run) Report {
	// Collect all scores, grand mean, and group by seed and by category.
	var all []float64
	seedScores := map[int64][]float64{}
	catScores := map[string][]float64{}
	for _, rn := range runs {
		for _, c := range rn.PerCase {
			all = append(all, c.Score)
			seedScores[rn.Seed] = append(seedScores[rn.Seed], c.Score)
			catScores[c.Category] = append(catScores[c.Category], c.Score)
		}
	}
	grand := mean(all)
	total := variance(all, grand)

	// Between-seed / between-item components: size-weighted variance of the group
	// means around the grand mean (a one-way ANOVA between-group estimate).
	seedGroups := make([][]float64, 0, len(seedScores))
	for _, v := range seedScores {
		seedGroups = append(seedGroups, v)
	}
	catGroups := make([][]float64, 0, len(catScores))
	for _, v := range catScores {
		catGroups = append(catGroups, v)
	}
	seedComp := betweenGroup(seedGroups, grand)
	itemComp := betweenGroup(catGroups, grand)
	resid := total - seedComp - itemComp
	if resid < 0 {
		resid = 0 // crossed components can slightly over-explain on unbalanced data
	}

	vc := VarianceComponents{Total: r6(total), Seed: r6(seedComp), Item: r6(itemComp), Residual: r6(resid)}
	if total > 0 {
		vc.SeedFrac = r6(seedComp / total)
		vc.ItemFrac = r6(itemComp / total)
		vc.ResidFrac = r6(resid / total)
	}

	facet := "residual"
	if seedComp >= itemComp && seedComp >= resid {
		facet = "seed"
	} else if itemComp >= seedComp && itemComp >= resid {
		facet = "item"
	}

	cats := make([]CategoryReliability, 0, len(catScores))
	for cat, ss := range catScores {
		m := mean(ss)
		cr := CategoryReliability{
			Category:       cat,
			N:              len(ss),
			Difficulty:     r6(m),
			Discrimination: r6(math.Sqrt(variance(ss, m))),
		}
		switch {
		case m >= saturatedAbove:
			cr.Flag = "saturated"
		case m <= floorBelow:
			cr.Flag = "floor"
		}
		cats = append(cats, cr)
	}
	sort.Slice(cats, func(i, j int) bool { return cats[i].Category < cats[j].Category })

	advice := adviceFor(facet)

	return Report{
		Runs:          len(runs),
		Cases:         len(all),
		GrandMean:     r6(grand),
		Variance:      vc,
		DominantFacet: facet,
		Categories:    cats,
		ParserSignals: parserSignals(runs),
		Advice:        advice,
	}
}

// plainRecallCats are the categories a deterministic parser answers by verbatim
// routing (the value is in the haystack); synthesisCats require aggregation,
// temporal/state reasoning, contradiction resolution, or grounded abstention —
// answers that are NOT a verbatim substring and that the v3 hardening made
// resistant to lexical shortcuts. A harness that aces the former and fails the
// latter has the parser signature: it retrieves but does not reason.
var plainRecallCats = map[string]bool{
	"single-session-recall": true, "preference": true, "assistant-recall": true,
	"preference-application": true,
}

var synthesisCats = map[string]bool{
	"aggregation-count": true, "computed-answer": true, "temporal-reasoning": true,
	"point-in-time": true, "multi-session": true, "contradiction": true,
	"abstention": true, "knowledge-update": true,
	// v5/v6 hard classes.
	"multi-hop-relational": true, "temporal-depth": true, "multi-query-recall": true,
	"nonverbatim-computed": true, "passive-consolidation": true,
	// v7 difficulty classes (reasoning-required; a parser collapses on these).
	"multi-hop-deep": true, "lifecycle-deep-read": true, "near-miss-abstention": true,
	"temporal-arithmetic": true, "injection-composed": true,
}

// ParserSignals is ADVISORY-ONLY telemetry that separates a deterministic
// string-table/regex harness from a genuine reasoner. It is deliberately NOT
// folded into any score: the composite must stay a pure, reproducible function
// of (dataset, transcript) — trustless — so these signals only route a harness
// to screening / audit, never change its number. The primary discriminator is
// the trap-conditioned accuracy GAP: a parser is near-perfect on plain recall
// and collapses on synthesis/trap families, while a reasoner degrades
// gracefully. LatencyFlatness (when latencies are present) is a secondary tell:
// a lookup returns in ~constant time regardless of difficulty.
type ParserSignals struct {
	Harnesses []HarnessSignal `json:"harnesses"`
	Note      string          `json:"note"`
}

// HarnessSignal is the per-harness (or whole-population when no harness id was
// supplied) parser-vs-reasoner summary.
type HarnessSignal struct {
	Harness        string  `json:"harness"`
	Runs           int     `json:"runs"`
	PlainRecallAcc float64 `json:"plain_recall_acc"`
	SynthesisAcc   float64 `json:"synthesis_acc"`
	TrapGap        float64 `json:"trap_gap"` // plain - synthesis; large positive = parser-like
	LatencyFlat    *bool   `json:"latency_flat,omitempty"`
	Flag           string  `json:"flag,omitempty"` // "parser-like" | ""
}

// parserLikeGap is the trap-gap above which, combined with high plain-recall
// accuracy, a harness is flagged parser-like for audit routing. This is advisory
// telemetry only and never enters the composite; it is a threshold in public
// code, not a secret.
const parserLikeGap = 0.35

// parserMinSamples is the minimum cases in EACH of the plain and synthesis
// buckets before the parser-like flag may be set, so a tiny sample cannot mint a
// spurious accuracy gap.
const parserMinSamples = 3

func parserSignals(runs []run) ParserSignals {
	type acc struct {
		plainSum, plainN float64
		synthSum, synthN float64
		runs             int
		easyLat, hardLat []float64 // plain vs synthesis latencies (difficulty proxy)
		sawLatency       bool
	}
	by := map[string]*acc{}
	for _, rn := range runs {
		a := by[rn.Harness]
		if a == nil {
			a = &acc{}
			by[rn.Harness] = a
		}
		a.runs++
		for _, c := range rn.PerCase {
			switch {
			case plainRecallCats[c.Category]:
				a.plainSum += c.Score
				a.plainN++
				if c.LatencyMs > 0 {
					a.easyLat = append(a.easyLat, c.LatencyMs)
					a.sawLatency = true
				}
			case synthesisCats[c.Category]:
				a.synthSum += c.Score
				a.synthN++
				if c.LatencyMs > 0 {
					a.hardLat = append(a.hardLat, c.LatencyMs)
					a.sawLatency = true
				}
			}
		}
	}
	ids := make([]string, 0, len(by))
	for id := range by {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	var out []HarnessSignal
	for _, id := range ids {
		a := by[id]
		if a.plainN == 0 || a.synthN == 0 {
			continue // need both buckets to compute a gap
		}
		plain := a.plainSum / a.plainN
		synth := a.synthSum / a.synthN
		gap := plain - synth
		hs := HarnessSignal{
			Harness:        id,
			Runs:           a.runs,
			PlainRecallAcc: r6(plain),
			SynthesisAcc:   r6(synth),
			TrapGap:        r6(gap),
		}
		// A parser aces plain recall AND collapses on synthesis. The flag REQUIRES
		// that accuracy gap on a sufficient sample. Latency flatness is recorded as
		// corroborating telemetry but is NOT sufficient on its own: a genuine
		// reasoner that answers plain and synthesis equally well at constant time
		// has a zero gap and must not be flagged.
		if a.plainN >= parserMinSamples && a.synthN >= parserMinSamples &&
			plain >= 0.8 && gap >= parserLikeGap {
			hs.Flag = "parser-like"
		}
		if a.sawLatency && len(a.easyLat) > 0 && len(a.hardLat) > 0 {
			flat := latencyFlat(mean(a.easyLat), mean(a.hardLat))
			hs.LatencyFlat = &flat
		}
		out = append(out, hs)
	}
	note := "advisory only: never folded into the composite; routes flagged harnesses to screening/audit"
	return ParserSignals{Harnesses: out, Note: note}
}

// latencyFlat reports whether synthesis-case latency is not meaningfully higher
// than plain-recall latency: a reasoner spends more on harder cases, a lookup
// returns in ~constant time. Flat when the harder bucket is within 20% of the
// easy bucket.
func latencyFlat(easyMean, hardMean float64) bool {
	if easyMean <= 0 {
		return false
	}
	return hardMean <= easyMean*1.2
}

func adviceFor(facet string) string {
	switch facet {
	case "seed":
		return "seed-dominated: increase the number of seeds per comparison (buy seeds); CRN paired scoring helps most here"
	case "item":
		return "item-dominated: increase cases per run or drop low-information (saturated/floor) categories (buy items)"
	default:
		return "residual-dominated: seed×item interaction or grading noise dominates; check grader reliability and per-category stability"
	}
}

// betweenGroup returns the size-weighted variance of group means around the grand
// mean: the between-group (facet) variance component of a one-way decomposition.
func betweenGroup(groups [][]float64, grand float64) float64 {
	var num float64
	var n int
	for _, g := range groups {
		if len(g) == 0 {
			continue
		}
		gm := mean(g)
		num += float64(len(g)) * (gm - grand) * (gm - grand)
		n += len(g)
	}
	if n == 0 {
		return 0
	}
	return num / float64(n)
}

func mean(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	var s float64
	for _, x := range xs {
		s += x
	}
	return s / float64(len(xs))
}

func variance(xs []float64, m float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	var s float64
	for _, x := range xs {
		d := x - m
		s += d * d
	}
	return s / float64(len(xs))
}

func r6(x float64) float64 { return math.Round(x*1e6) / 1e6 }
