// Command vstudy is the offline v7 variance-study driver (see
// docs/v7-variance-study.md). It generates many full-profile datasets for
// bench_version 6 and 7 and scores fixed, deterministic harness strategies
// against each one, producing:
//
//   - per-strategy composite (0.5*tool_mean + 0.5*memory_mean) mean / SD /
//     quantiles across seeds — the seed-to-seed spread the leaderboard's
//     hysteresis + protection-margin machinery lives on top of;
//   - gstudy-format JSONL per (version, strategy) so cmd/gstudy can run the
//     G-study variance decomposition and per-category reliability pass;
//   - a paired equal-skill comparison per model tier (two independent draws
//     of the same strategy on the SAME seeds) that measures the CRN
//     confirmation-seed noise floor directly;
//   - per-category contribution to composite variance for the champW
//     decision-boundary tier, to find case families that dominate the spread.
//
// Strategies are deterministic pure functions of (dataset, salt): the naive
// tiers replicate the gen package's difficulty-measurement strategies
// (parrot / overlap / recency / dump / abstain memory answers + the fixed
// keyword tool router), and the mid tier ("strong") is the oracle downgraded
// by a deterministic per-case error draw keyed to case type — a stand-in for
// a realistic top miner near the champion decision boundary. Everything is
// LLM-free and byte-reproducible; nothing here touches generation bytes.
//
// Usage: vstudy -seeds 300 -out /tmp/vstudy
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"hash/fnv"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	seeds := flag.Int("seeds", 300, "number of dataset seeds per bench version")
	firstSeed := flag.Int64("first-seed", 1, "first seed (seeds are first..first+n-1)")
	outDir := flag.String("out", "", "directory for gstudy-format JSONL outputs (empty = skip)")
	margin := flag.Float64("margin", 0.007, "live-fold protection margin in composite points")
	flag.Parse()

	if *outDir != "" {
		if err := os.MkdirAll(*outDir, 0o755); err != nil {
			fmt.Fprintln(os.Stderr, "mkdir:", err)
			os.Exit(1)
		}
	}

	summary := map[string]any{}
	for _, bv := range []int{protocol.BenchVersionV6, protocol.BenchVersionV7} {
		res := runVersion(bv, *firstSeed, *seeds, *outDir)
		summary[fmt.Sprintf("v%d", bv)] = res.summarize(*margin)
	}
	b, _ := json.MarshalIndent(summary, "", "  ")
	fmt.Println(string(b))
}

// ---------------------------------------------------------------------------
// Strategy simulation
// ---------------------------------------------------------------------------

// strategyNames in report order. parrot/overlap/recency/dump/abstain are the
// naive memory tiers (each paired with the keyword tool router); strong is the
// study-v1 mid-tier miner model (kept for continuity across study revisions);
// champS/champW are the deepening's champion-tier anchors (the strong
// newDitto-like and weak whitycatboss/infinity-like harness models from
// gen.TestV7ChampionTierLandsNearTarget, ported here because test symbols are
// not importable); uniform is the rate-assumption control (every case fails
// with the same p=0.10); oracle is the ceiling. Every model tier also runs an
// independent equal-skill twin (salt B) for the paired CRN comparison.
var strategyNames = []string{
	"parrot", "overlap", "recency", "dump", "abstain",
	"strong", "champS", "champW", "uniform", "oracle",
}

// modelTiers are the hash-error simulated tiers: name -> per-case error-rate
// functions plus the two salts of the equal-skill twin pair. Salts are chosen
// so the study-v1 "strong"/"uniform" draws reproduce byte-identically.
type tierRates struct {
	mem          func(qt string) float64
	tool         func(cat string) float64
	saltA, saltB string
}

var modelTiers = map[string]tierRates{
	"strong":  {mem: memErrRate, tool: toolErrRate, saltA: "A", saltB: "B"},
	"uniform": {mem: flatErr, tool: flatErr, saltA: "U", saltB: "UB"},
	"champS":  {mem: champErrMemS, tool: champErrToolS, saltA: "CS-A", saltB: "CS-B"},
	"champW":  {mem: champErrMemW, tool: champErrToolW, saltA: "CW-A", saltB: "CW-B"},
}

func flatErr(string) float64 { return uniformErr }

// seedResult is one strategy's outcome on one dataset seed.
type seedResult struct {
	Composite float64
	MemMean   float64
	ToolMean  float64
}

// caseScore is one scored case, gstudy-format.
type caseScore struct {
	Category string  `json:"category"`
	Score    float64 `json:"score"`
}

// versionResult accumulates everything measured for one bench version.
type versionResult struct {
	BenchVersion int
	Seeds        []int64
	// per strategy -> per seed
	Runs map[string][]seedResult
	// PairB: model tier -> per-seed composite of the salt-B equal-skill twin.
	PairB map[string][]float64
	// Exp: model tier -> per-seed EXPECTED composite (sum of 1-errRate, no
	// Bernoulli draws): its across-seed SD is the pure STRUCTURAL (case-mix
	// difficulty) component of that tier's variance.
	Exp map[string][]float64
	// ExpFlat: model tier -> per-seed expected FLAT mean (all cases pooled,
	// no 0.5/0.5 side weighting) — comparable to the champion-tier anchor
	// numbers in gen.TestV7ChampionTierLandsNearTarget.
	ExpFlat map[string][]float64
	// per strategy -> per seed -> per case (for gstudy JSONL).
	Cases map[string][][]caseScore
	// per seed: category -> weighted contribution to the composite
	// (0.5*n_cat/N_suite * cat_mean), champW (decision-boundary tier) only.
	BoundaryCatContrib []map[string]float64
	OracleFailures     int
}

func runVersion(bv int, first int64, n int, outDir string) *versionResult {
	prof, _ := gen.ProfileForVersion("full", bv)
	vr := &versionResult{
		BenchVersion: bv,
		Runs:         map[string][]seedResult{},
		Cases:        map[string][][]caseScore{},
		PairB:        map[string][]float64{},
		Exp:          map[string][]float64{},
		ExpFlat:      map[string][]float64{},
	}
	for i := 0; i < n; i++ {
		seed := first + int64(i)
		a, err := gen.GenerateDataset(seed, prof, bv)
		if err != nil {
			fmt.Fprintf(os.Stderr, "generate v%d seed %d: %v\n", bv, seed, err)
			os.Exit(1)
		}
		vr.Seeds = append(vr.Seeds, seed)
		evalSeed(vr, a, bv, seed)
	}
	if outDir != "" {
		for _, s := range []string{"overlap", "strong", "champS", "champW", "uniform", "oracle"} {
			writeGstudyJSONL(filepath.Join(outDir, fmt.Sprintf("runs_v%d_%s.jsonl", bv, s)), vr.Seeds, vr.Cases[s], s)
		}
	}
	return vr
}

// evalSeed scores every strategy against one generated dataset.
func evalSeed(vr *versionResult, a gen.DatasetArtifact, bv int, seed int64) {
	// --- memory side ---------------------------------------------------------
	type wavePair struct {
		wave int
		pair protocol.MemoryPair
	}
	byUser := map[string][]wavePair{}
	for _, w := range a.MemoryWaves {
		user := w.UserID
		if user == "" {
			user = gen.PrimaryUser
		}
		for _, p := range w.Pairs {
			byUser[user] = append(byUser[user], wavePair{wave: w.Wave, pair: p})
		}
	}

	memScores := map[string][]caseScore{} // strategy -> per-case
	pairBMem := map[string]float64{}      // tier -> B-twin score sum
	expMemT := map[string]float64{}       // tier -> expected score sum
	catN := map[string]int{}              // champW category sizes
	catSum := map[string]float64{}        // champW per-category score sums
	for _, c := range a.MemoryCases {
		user := c.UserID
		if user == "" {
			user = gen.PrimaryUser
		}
		var avail []protocol.MemoryPair
		for _, wp := range byUser[user] {
			if wp.wave <= c.RunAfterWave {
				avail = append(avail, wp.pair)
			}
		}
		naive := naiveMemoryAnswers(c.Question, avail)
		for strat, ans := range naive {
			sc := grade.Memory(c.MemoryCase, protocol.RunResponse{FinalText: ans}).Score
			memScores[strat] = append(memScores[strat], caseScore{Category: c.QuestionType, Score: sc})
		}
		absSc := grade.Memory(c.MemoryCase, protocol.RunResponse{Abstain: true, FinalText: "I don't have that information."}).Score
		memScores["abstain"] = append(memScores["abstain"], caseScore{Category: c.QuestionType, Score: absSc})

		oSc := grade.Memory(c.MemoryCase, oracleResponse(c.MemoryCase)).Score
		if oSc != 1 {
			vr.OracleFailures++
		}
		memScores["oracle"] = append(memScores["oracle"], caseScore{Category: c.QuestionType, Score: oSc})

		key := fmt.Sprintf("%d|%d|mem|%s|%s", bv, seed, c.ID, c.QuestionID)
		for tier, tr := range modelTiers {
			e := tr.mem(c.QuestionType)
			sA := errDraw(key, tr.saltA, e) * oSc
			memScores[tier] = append(memScores[tier], caseScore{Category: c.QuestionType, Score: sA})
			pairBMem[tier] += errDraw(key, tr.saltB, e) * oSc
			expMemT[tier] += 1 - e
			if tier == "champW" {
				catN["mem:"+c.QuestionType]++
				catSum["mem:"+c.QuestionType] += sA
			}
		}
	}

	// --- tool side -----------------------------------------------------------
	toolScores := map[string][]caseScore{} // router / tiers / oracle
	pairBTool := map[string]float64{}
	expToolT := map[string]float64{}
	for _, c := range a.ToolCases {
		routed := keywordRoute(c.Prompt)
		rSc := 0.0
		switch {
		case len(c.ExpectedTools) == 0:
			if routed == "" {
				rSc = 1
			}
		case len(c.ExpectedTools) == 1:
			if routed == c.ExpectedTools[0].Name {
				rSc = 1
			}
		}
		toolScores["router"] = append(toolScores["router"], caseScore{Category: c.Category, Score: rSc})
		toolScores["oracle"] = append(toolScores["oracle"], caseScore{Category: c.Category, Score: 1})

		key := fmt.Sprintf("%d|%d|tool|%s", bv, seed, c.ID)
		for tier, tr := range modelTiers {
			e := tr.tool(c.Category)
			sA := errDraw(key, tr.saltA, e)
			toolScores[tier] = append(toolScores[tier], caseScore{Category: c.Category, Score: sA})
			pairBTool[tier] += errDraw(key, tr.saltB, e)
			expToolT[tier] += 1 - e
			if tier == "champW" {
				catN["tool:"+c.Category]++
				catSum["tool:"+c.Category] += sA
			}
		}
	}

	// --- assemble per-strategy runs ------------------------------------------
	caseMean := func(cs []caseScore) float64 {
		s := 0.0
		for _, c := range cs {
			s += c.Score
		}
		return s / float64(len(cs))
	}
	nMem, nTool := float64(len(a.MemoryCases)), float64(len(a.ToolCases))
	for _, strat := range strategyNames {
		tool := toolScores[strat]
		if tool == nil {
			tool = toolScores["router"] // naive memory tiers ride the keyword router
		}
		mm, tm := caseMean(memScores[strat]), caseMean(tool)
		vr.Runs[strat] = append(vr.Runs[strat], seedResult{
			Composite: 0.5*tm + 0.5*mm, MemMean: mm, ToolMean: tm,
		})
		all := append(append([]caseScore{}, memScores[strat]...), tool...)
		vr.Cases[strat] = append(vr.Cases[strat], all)
	}
	for tier := range modelTiers {
		vr.PairB[tier] = append(vr.PairB[tier], 0.5*pairBTool[tier]/nTool+0.5*pairBMem[tier]/nMem)
		vr.Exp[tier] = append(vr.Exp[tier], 0.5*expToolT[tier]/nTool+0.5*expMemT[tier]/nMem)
		vr.ExpFlat[tier] = append(vr.ExpFlat[tier], (expMemT[tier]+expToolT[tier])/(nMem+nTool))
	}

	// champW per-category composite contribution: w_c*mean_c with
	// w_c = 0.5*n_c/N_side.
	contrib := map[string]float64{}
	for cat := range catN {
		side := nMem
		if strings.HasPrefix(cat, "tool:") {
			side = nTool
		}
		contrib[cat] = 0.5 * catSum[cat] / side
	}
	vr.BoundaryCatContrib = append(vr.BoundaryCatContrib, contrib)
}

// ---------------------------------------------------------------------------
// Mid-tier ("strong") miner model: deterministic per-case error draws
// ---------------------------------------------------------------------------

// uniformErr is the flat error rate of the rate-assumption control strategy.
const uniformErr = 0.10

// errDraw returns 1 (pass) or 0 (fail) from a deterministic hash of the case
// key + salt: u < rate fails. Same seed re-run reproduces identically (noise
// floor 0); across cases/seeds the draws behave as independent Bernoullis.
func errDraw(key, salt string, rate float64) float64 {
	h := fnv.New64a()
	h.Write([]byte(salt + "|" + key))
	u := float64(h.Sum64()>>11) / float64(1<<53)
	if u < rate {
		return 0
	}
	return 1
}

// memErrRates models a strong-but-imperfect top miner: near-perfect on plain
// recall, mid-single-digit slips on routine reasoning, and materially higher
// error on the deep v5–v7 classes. The exact values are documented modeling
// assumptions (docs/v7-variance-study.md); the "uniform" strategy is the
// control that removes them.
var memErrRates = map[string]float64{
	// plain recall / writes / floor cases
	"single-session-recall": 0.03, "preference": 0.03, "assistant-recall": 0.04,
	"memory-write": 0.02, "memory-write-read": 0.05, "declarative-write": 0.02,
	"canary": 0.02, "conversational-chitchat": 0.02, "conversational-declarative": 0.03,
	"stored-instruction-benign": 0.05, "composed-note-benign": 0.08,
	// routine reasoning / behavior
	"abstention": 0.08, "conversational-abstention": 0.08, "isolation": 0.08,
	"knowledge-update": 0.10, "contradiction": 0.12, "multi-session": 0.12,
	"temporal-reasoning": 0.12, "point-in-time": 0.12, "preference-application": 0.10,
	"aggregation-count": 0.12, "computed-answer": 0.12, "declarative-behavior": 0.10,
	"declarative-write-read": 0.08, "injection-resistance": 0.08,
	"injection-stored-instruction": 0.10,
	// v5/v6 hard classes
	"multi-hop-relational": 0.18, "temporal-depth": 0.18, "multi-query-recall": 0.18,
	"nonverbatim-computed": 0.18, "passive-consolidation": 0.18,
	// v7 difficulty classes
	"lifecycle-deep-write": 0.04, "lifecycle-deep-read": 0.30, "multi-hop-deep": 0.30,
	"near-miss-abstention": 0.25, "temporal-arithmetic": 0.30, "injection-composed": 0.20,
}

func memErrRate(qt string) float64 {
	if e, ok := memErrRates[qt]; ok {
		return e
	}
	return 0.10
}

// toolErrRates: same modeling idea for the tool suite.
var toolErrRates = map[string]float64{
	"negation_no_tool": 0.12, "stale_context_web": 0.20,
	"link_chain_result_usage": 0.28, "job_chain_recovery_result_usage": 0.28,
	"job_chain_result_usage": 0.20, "web_recovery_result_usage": 0.20,
	"web_result_usage": 0.18, "multi_web_result_usage": 0.18,
	"arg_hallucination": 0.10, "abstention": 0.08,
}

func toolErrRate(cat string) float64 {
	if e, ok := toolErrRates[cat]; ok {
		return e
	}
	if strings.HasPrefix(cat, "multi_") || strings.HasPrefix(cat, "parallel_") {
		return 0.15
	}
	if strings.Contains(cat, "_not_") {
		return 0.10
	}
	return 0.05
}

// ---------------------------------------------------------------------------
// Champion-tier anchors (study v2, deepened suite)
//
// Ported verbatim from gen/v7difficulty_test.go's champion-tier calibration
// (TestV7ChampionTierLandsNearTarget): per-class expected PASS rates fixed to
// reproduce the operator's top-5 leaderboard rebench. champS is the strong
// anchor (newDitto-like, ~0.83 flat mean on the pre-deepening suite); champW
// derives the weak anchor (whitycatboss/infinity-like) through the same
// square-ish falloff. Test symbols are not importable, so the tables are
// duplicated here; TestChampionTablesMatchAnchors pins the calibration
// outcome so silent drift between the copies is caught.
// ---------------------------------------------------------------------------

var champStrongMem = map[string]float64{
	"single-session-recall": 0.97, "preference": 0.97, "assistant-recall": 0.95, "canary": 0.90,
	"multi-session": 0.82, "temporal-reasoning": 0.75, "point-in-time": 0.72,
	"contradiction": 0.80, "knowledge-update": 0.80, "aggregation-count": 0.72,
	"computed-answer": 0.55, "preference-application": 0.75, "abstention": 0.85,
	"conversational-chitchat": 1.0, "conversational-declarative": 0.92, "conversational-abstention": 0.80,
	"declarative-write": 1.0, "declarative-write-read": 0.82, "declarative-behavior": 0.75,
	"memory-write": 0.95, "memory-write-read": 0.85, "lifecycle-deep-write": 0.95, "lifecycle-deep-read": 0.10,
	"injection-resistance": 0.70, "injection-stored-instruction": 0.62, "stored-instruction-benign": 0.85,
	"injection-composed": 0.30, "composed-note-benign": 0.82, "isolation": 0.55,
	"multi-hop-relational": 0.50, "temporal-depth": 0.50, "multi-query-recall": 0.62,
	"nonverbatim-computed": 0.42, "passive-consolidation": 0.60,
	"multi-hop-deep": 0.06, "near-miss-abstention": 0.10, "temporal-arithmetic": 0.10,
	"subscription-own": 0.35, "subscription-attributed": 0.45,
}

var champStrongTool = map[string]float64{
	"route_memory_not_web": 0.80, "route_web_not_memory": 0.80, "agent_run_not_read": 0.80,
	"agent_read_not_run": 0.80, "image_edit_not_create": 0.80, "workflow_not_job": 0.75,
	"automation_not_job": 0.75, "memory_save_not_search": 0.85, "arg_hallucination": 0.80,
	"negation_no_tool": 0.30, "stale_context_web": 0.50, "tool_discovery": 0.80,
	"code_compute_not_agent_job": 0.80,
	"web_result_usage":           0.70, "multi_web_result_usage": 0.50, "web_recovery_result_usage": 0.55,
	"job_chain_result_usage": 0.50, "job_chain_recovery_result_usage": 0.40, "link_chain_result_usage": 0.25,
	"multi_web_read": 0.85, "multi_subject_scope": 0.85, "multi_job_status": 0.85,
	"multi_image_edit": 0.85, "parallel_web_image": 0.85, "entity_lookup_chain": 0.55,
}

// champWeakRate maps a strong-anchor pass rate to the weak anchor: near-perfect
// classes barely move, mid/hard classes fall off sharply.
func champWeakRate(strong float64) float64 {
	w := strong * strong * (0.55 + 0.45*strong)
	if w < 0 {
		w = 0
	}
	return w
}

func champPassMemS(qt string) float64 {
	if v, ok := champStrongMem[qt]; ok {
		return v
	}
	return 0.97
}

func champPassToolS(cat string) float64 {
	if v, ok := champStrongTool[cat]; ok {
		return v
	}
	return 0.95
}

// The tier interface works in ERROR rates.
func champErrMemS(qt string) float64   { return 1 - champPassMemS(qt) }
func champErrToolS(cat string) float64 { return 1 - champPassToolS(cat) }
func champErrMemW(qt string) float64   { return 1 - champWeakRate(champPassMemS(qt)) }
func champErrToolW(cat string) float64 { return 1 - champWeakRate(champPassToolS(cat)) }

// oracleResponse is the canonical correct response keyed on AnswerKind
// (mirrors the gen package's oracle-solvability sweep).
func oracleResponse(mc protocol.MemoryCase) protocol.RunResponse {
	switch mc.AnswerKind {
	case protocol.AnswerDecline:
		return protocol.RunResponse{Abstain: true, FinalText: "I don't have that on record."}
	case protocol.AnswerAcknowledge:
		return protocol.RunResponse{FinalText: "Done — I've removed that from my records."}
	case protocol.AnswerChitchat:
		return protocol.RunResponse{FinalText: "Hey! Good to hear from you — how's your day going?"}
	}
	return protocol.RunResponse{FinalText: mc.ExpectedAnswer}
}

// ---------------------------------------------------------------------------
// Naive memory strategies + keyword router (replicates gen's difficulty sweep)
// ---------------------------------------------------------------------------

var naiveStopwords = map[string]bool{
	"the": true, "and": true, "for": true, "you": true, "your": true,
	"what": true, "whats": true, "that": true, "this": true, "did": true,
	"was": true, "were": true, "with": true, "have": true, "has": true,
	"how": true, "are": true, "its": true, "not": true, "now": true,
	"one": true, "quick": true, "hey": true, "remind": true, "again": true,
	"say": true, "said": true, "tell": true, "told": true, "which": true,
	"who": true, "where": true, "when": true, "does": true, "still": true,
}

func naiveTokens(s string) []string {
	var out []string
	var b strings.Builder
	flush := func() {
		w := b.String()
		b.Reset()
		if len(w) >= 3 && !naiveStopwords[w] {
			out = append(out, w)
		}
	}
	for _, r := range strings.ToLower(s) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		} else {
			flush()
		}
	}
	flush()
	return out
}

// naiveMemoryAnswers computes the parrot/overlap/recency/dump answers for one
// question given the pairs available at its unlock wave.
func naiveMemoryAnswers(question string, avail []protocol.MemoryPair) map[string]string {
	qTok := naiveTokens(question)
	bestOverlap, bestIdx := -1, -1
	recIdx := -1
	var dumpAll strings.Builder
	for i, p := range avail {
		text := p.Prompt + " " + p.Response
		dumpAll.WriteString(text)
		dumpAll.WriteString(" ")
		pTok := map[string]bool{}
		for _, w := range naiveTokens(text) {
			pTok[w] = true
		}
		overlap := 0
		for _, w := range qTok {
			if pTok[w] {
				overlap++
			}
		}
		if overlap > bestOverlap {
			bestOverlap, bestIdx = overlap, i
		}
		if overlap > 0 && (recIdx < 0 || avail[i].Timestamp >= avail[recIdx].Timestamp) {
			recIdx = i
		}
	}
	out := map[string]string{"parrot": question, "dump": dumpAll.String(), "overlap": "", "recency": ""}
	if bestIdx >= 0 {
		out["overlap"] = avail[bestIdx].Prompt + " " + avail[bestIdx].Response
	}
	if recIdx >= 0 {
		out["recency"] = avail[recIdx].Prompt + " " + avail[recIdx].Response
	}
	return out
}

type routerCue struct {
	subs []string
	tool string
}

var routerCues = []routerCue{
	{[]string{"remember that", "note for later", "keep in mind that", "please keep in mind"}, "save_memory"},
	{[]string{"delete memory", "forget what's in", "remove memory"}, "delete_memory"},
	{[]string{"update memory", "change what you saved", "correct memory"}, "update_memory"},
	{[]string{"subjects", "which of my subjects", "topic that covers"}, "search_subjects"},
	{[]string{"what did i say about", "do you remember", "from my memories", "i mentioned it before", "i saved", "i told you"}, "search_memories"},
	{[]string{"http"}, "read_links"},
	{[]string{"image", "picture", "draw"}, "create_image"},
	{[]string{"artifact", "build me", "make me"}, "artifacts"},
	{[]string{"workflow", "parallel"}, "execute_agent_workflow"},
	{[]string{"background job", "kick off", "dispatch", "run a background"}, "execute_agent_job"},
	{[]string{"job status", "jobs"}, "list_agent_jobs"},
	{[]string{"theme"}, "set_theme"},
	{[]string{"model"}, "set_main_model"},
	{[]string{"reasoning", "effort", "thinking level"}, "set_reasoning_effort"},
	{[]string{"tool preferences", "chat tools", "which tools"}, "set_chat_tool_preferences"},
	{[]string{"accent"}, "set_accent_color"},
	{[]string{"font", "typeface"}, "set_chat_font"},
	{[]string{"recipe"}, "apply_recipe"},
	{[]string{"every morning", "every monday", "on a schedule", "each weekday"}, "create_automation"},
	{[]string{"calendar"}, "calendar_create_event"},
	{[]string{"email", "send an email"}, "gmail_send"},
	{[]string{"feedback", "ditto team", "the devs"}, "file_feedback_for_team"},
	{[]string{"search the web", "latest", "news", "current", "google", "up-to-date", "search for", "look up"}, "search_web"},
	{[]string{"what can you do", "capabilities"}, "discover_capabilities"},
}

func keywordRoute(prompt string) string {
	p := strings.ToLower(prompt)
	for _, cue := range routerCues {
		for _, sub := range cue.subs {
			if strings.Contains(p, sub) {
				return cue.tool
			}
		}
	}
	return ""
}

// ---------------------------------------------------------------------------
// Summaries
// ---------------------------------------------------------------------------

// StratSummary is one strategy's across-seed distribution.
type StratSummary struct {
	N         int       `json:"n"`
	Mean      float64   `json:"mean"`
	SD        float64   `json:"sd"`        // between-seed SD of the composite (= single-seed SE)
	SEofMean  float64   `json:"se_mean"`   // SD/sqrt(N): precision of the reported mean
	Quantiles []float64 `json:"quantiles"` // min, p5, p25, p50, p75, p95, max
	MemMean   float64   `json:"mem_mean"`
	MemSD     float64   `json:"mem_sd"`
	ToolMean  float64   `json:"tool_mean"`
	ToolSD    float64   `json:"tool_sd"`
}

// PairedSummary is the equal-skill CRN comparison (strong vs strongB).
type PairedSummary struct {
	DiffMean       float64 `json:"diff_mean"`
	DiffSD         float64 `json:"diff_sd"` // paired same-seed noise floor
	FracOverMargin float64 `json:"frac_abs_diff_over_margin"`
	// NormalProbOverMargin is 2*(1-Phi(margin/DiffSD)): the model-based
	// probability two equal-skill agents differ by more than the protection
	// margin on one shared seed.
	NormalProbOverMargin float64 `json:"normal_prob_over_margin"`
	// UnpairedProbOverMargin uses sqrt(2)*SD(strong composite): the same
	// probability when the two agents are scored on DIFFERENT single seeds.
	UnpairedProbOverMargin float64            `json:"unpaired_prob_over_margin"`
	SeedsToResolve         map[string]SeedReq `json:"seeds_to_resolve"`
}

// SeedReq is the confirmation-seed requirement for one true gap.
type SeedReq struct {
	// Detect50: seeds so that the 1.64*SE band equals the gap (50% power at
	// one-sided alpha=0.05 — the current band just barely resolves it).
	Detect50 int `json:"detect_50pct_power"`
	// Power95: seeds for 95% power at one-sided alpha=0.05.
	Power95 int `json:"power_95pct"`
}

// CatVar is one category's contribution to composite variance.
type CatVar struct {
	Category string  `json:"category"`
	VarShare float64 `json:"var_share"` // Var(w_c*mean_c) / sum over categories
	SD       float64 `json:"sd_of_contribution"`
}

func (vr *versionResult) summarize(margin float64) map[string]any {
	out := map[string]any{
		"seeds":           len(vr.Seeds),
		"oracle_failures": vr.OracleFailures,
	}
	strat := map[string]StratSummary{}
	for name, runs := range vr.Runs {
		var comp, mem, tool []float64
		for _, r := range runs {
			comp = append(comp, r.Composite)
			mem = append(mem, r.MemMean)
			tool = append(tool, r.ToolMean)
		}
		cm, cs := meanSD(comp)
		mm, ms := meanSD(mem)
		tm, ts := meanSD(tool)
		strat[name] = StratSummary{
			N: len(comp), Mean: r4(cm), SD: r4(cs), SEofMean: r4(cs / math.Sqrt(float64(len(comp)))),
			Quantiles: quantiles(comp),
			MemMean:   r4(mm), MemSD: r4(ms), ToolMean: r4(tm), ToolSD: r4(ts),
		}
	}
	out["strategies"] = strat

	// Paired equal-skill comparison + structural split, per model tier.
	paired := map[string]PairedSummary{}
	structural := map[string]map[string]float64{}
	for tier := range modelTiers {
		runs := vr.Runs[tier]
		var diffs []float64
		over := 0
		for i, r := range runs {
			d := r.Composite - vr.PairB[tier][i]
			diffs = append(diffs, d)
			if math.Abs(d) > margin {
				over++
			}
		}
		dm, ds := meanSD(diffs)
		_, ss := meanSD(compositesOf(runs))
		ps := PairedSummary{
			DiffMean:               r4(dm),
			DiffSD:                 r4(ds),
			FracOverMargin:         r4(float64(over) / float64(len(diffs))),
			NormalProbOverMargin:   r4(2 * (1 - phi(margin/ds))),
			UnpairedProbOverMargin: r4(2 * (1 - phi(margin/(math.Sqrt2*ss)))),
			SeedsToResolve:         map[string]SeedReq{},
		}
		for _, gap := range []float64{0.005, 0.01, 0.02} {
			ps.SeedsToResolve[fmt.Sprintf("%.3f", gap)] = SeedReq{
				Detect50: seedsFor(ds, gap, 1.645, 0),
				Power95:  seedsFor(ds, gap, 1.645, 1.645),
			}
		}
		paired[tier] = ps

		em, es := meanSD(vr.Exp[tier])
		fm, _ := meanSD(vr.ExpFlat[tier])
		structural[tier] = map[string]float64{
			"expected_mean":      r4(em),
			"expected_flat_mean": r4(fm), // anchor-comparable pooled mean
			"structural_sd":      r4(es), // SD of the expected composite across seeds
			"bernoulli_sd":       r4(ds / math.Sqrt2),
			"total_sd":           r4(ss),
		}
	}
	out["paired_equal_skill"] = paired
	out["structural"] = structural

	// Per-category variance contribution (champW, the boundary tier).
	cats := map[string][]float64{}
	for _, m := range vr.BoundaryCatContrib {
		for c, v := range m {
			cats[c] = append(cats[c], v)
		}
	}
	var cvs []CatVar
	totalVar := 0.0
	varOf := map[string]float64{}
	for c, xs := range cats {
		for len(xs) < len(vr.Seeds) {
			xs = append(xs, 0) // category absent on a seed contributes 0
		}
		_, sd := meanSD(xs)
		varOf[c] = sd * sd
		totalVar += sd * sd
	}
	for c, v := range varOf {
		cvs = append(cvs, CatVar{Category: c, VarShare: r4(v / totalVar), SD: r4(math.Sqrt(v))})
	}
	sort.Slice(cvs, func(i, j int) bool { return cvs[i].VarShare > cvs[j].VarShare })
	if len(cvs) > 15 {
		cvs = cvs[:15]
	}
	out["champW_top_category_var_shares"] = cvs
	return out
}

func compositesOf(rs []seedResult) []float64 {
	var out []float64
	for _, r := range rs {
		out = append(out, r.Composite)
	}
	return out
}

// seedsFor returns the confirmation-seed count for a paired comparison with
// per-seed diff SD sd to resolve a true gap at one-sided z_alpha with power
// z_beta: n = ((z_alpha+z_beta)*sd/gap)^2, minimum 1.
func seedsFor(sd, gap, zAlpha, zBeta float64) int {
	n := math.Ceil(math.Pow((zAlpha+zBeta)*sd/gap, 2))
	if n < 1 {
		n = 1
	}
	return int(n)
}

func phi(x float64) float64 { return 0.5 * (1 + math.Erf(x/math.Sqrt2)) }

func meanSD(xs []float64) (float64, float64) {
	if len(xs) == 0 {
		return 0, 0
	}
	m := 0.0
	for _, x := range xs {
		m += x
	}
	m /= float64(len(xs))
	v := 0.0
	for _, x := range xs {
		v += (x - m) * (x - m)
	}
	v /= float64(len(xs) - 1)
	return m, math.Sqrt(v)
}

func quantiles(xs []float64) []float64 {
	s := append([]float64{}, xs...)
	sort.Float64s(s)
	q := func(p float64) float64 {
		i := p * float64(len(s)-1)
		lo := int(math.Floor(i))
		hi := int(math.Ceil(i))
		frac := i - float64(lo)
		return r4(s[lo]*(1-frac) + s[hi]*frac)
	}
	return []float64{q(0), q(0.05), q(0.25), q(0.5), q(0.75), q(0.95), q(1)}
}

func r4(x float64) float64 { return math.Round(x*1e4) / 1e4 }

// writeGstudyJSONL emits gstudy-format scored runs.
func writeGstudyJSONL(path string, seeds []int64, runs [][]caseScore, harness string) {
	f, err := os.Create(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "create:", err)
		os.Exit(1)
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	for i, cs := range runs {
		_ = enc.Encode(map[string]any{"seed": seeds[i], "harness": harness, "per_case": cs})
	}
}
