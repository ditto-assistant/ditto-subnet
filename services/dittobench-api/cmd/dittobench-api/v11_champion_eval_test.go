package main

// TEMPORARY local evaluation harness (not part of the shipped stack).
//
// It emulates the measured v10 champion's deterministic memory rule-engine core
// for the open-program family — the exact surface Bench v11 hardens — and grades
// its answers with the real scorer.GradeMemory. The champion's residual model
// (gpt-oss-20b) is NOT reproduced here (no local inference relay), so the number
// is a LOWER BOUND on how much the rule engine loses: the model can only recover
// some of the collapse, never widen the v10 lead.
//
// Run: go test ./cmd/dittobench-api/ -run TestV11ChampionStrategyCollapse -v -count=1

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// championEngine encodes the documented v10 rule-engine tactics:
//  1. bind field roles by matching v10's FIXED glossary role-phrases;
//  2. resolve the subject by literal alias echo from the question;
//  3. evaluate the single hardcoded balance program (approved − paid);
//  4. emit the integer minor-unit value into the answer slot.
//
// When any step fails it abstains (empty answer) — representing the rule engine
// handing a schema it cannot parse to the weak model, which scores near zero on
// a freshly composed program.
type championEngine struct {
	pairs map[string]protocol.MemoryPair
}

var (
	kvNum      = regexp.MustCompile(`([A-Za-z]+)=(-?\d{3,})`)
	kvWord     = regexp.MustCompile(`([A-Za-z]+)=([A-Za-z][A-Za-z0-9]+)`)
	decoyStrip = regexp.MustCompile(`unrelated[^\n]*`)
)

// v10 glossary role anchors — the literal phrases the champion keys on.
var v10RoleAnchors = map[string][]string{
	"approved": {"the later approved replacement", "approved replacement"},
	"paid":     {"is a settled payment", "settled payment"},
}

func newChampionEngine(waves []protocol.SeedRequest) championEngine {
	pairs := map[string]protocol.MemoryPair{}
	for _, w := range waves {
		for _, p := range w.Pairs {
			pairs[p.PairID] = p
		}
	}
	return championEngine{pairs: pairs}
}

// answer returns the champion's slot answer for one open-program case, or "" to
// abstain. evidenceIDs are the case's reviewer-bound evidence pairs.
func (e championEngine) answer(question string, evidenceIDs []string) string {
	var glossary strings.Builder
	var body strings.Builder
	for _, id := range evidenceIDs {
		p, ok := e.pairs[id]
		if !ok {
			continue
		}
		glossary.WriteString(p.Prompt)
		glossary.WriteString("\n")
		// Strip decoy segments ("unrelated <entity>=..., <label>=<decoy>") that
		// the champion's world_bind ignores, and flatten renderer delimiters so
		// email/table/ops rows parse the same as conversation rows.
		clean := decoyStrip.ReplaceAllString(p.Prompt, " ")
		body.WriteString(clean)
		body.WriteString("\n")
	}
	gloss := glossary.String()
	text := body.String()

	// Step 1: bind approved/paid labels via the fixed v10 glossary phrasing.
	approvedLabel := bindRole(gloss, v10RoleAnchors["approved"])
	paidLabel := bindRole(gloss, v10RoleAnchors["paid"])
	if approvedLabel == "" || paidLabel == "" {
		return "" // composed v11 glossary: role phrases absent → cannot bind
	}

	// Step 2: resolve the subject by literal alias echo from the question.
	// v11's descriptive binding removes the alias from the question.
	if aliasEcho(question, text) == "" {
		return ""
	}

	// Step 3: evaluate the single hardcoded program: approved − paid.
	approved, okA := readLabel(text, approvedLabel)
	paid, okP := readLabel(text, paidLabel)
	if !okA || !okP {
		return ""
	}
	return formatMinorUnits(approved - paid)
}

// formatMinorUnits renders a cents integer as the dollars.cents decimal the
// grader's money matcher expects (mirroring the champion's whitycat money
// normalizer). A bare integer would be read as whole dollars (×100) and miss.
func formatMinorUnits(cents int) string {
	if cents < 0 {
		return ""
	}
	return strconv.Itoa(cents/100) + "." + fmt.Sprintf("%02d", cents%100)
}

func bindRole(gloss string, anchors []string) string {
	lower := strings.ToLower(gloss)
	for _, anchor := range anchors {
		idx := strings.Index(lower, strings.ToLower(anchor))
		if idx < 0 {
			continue
		}
		// The label is the last KV-style/token label named just before the
		// anchor phrase, exactly as the v10 glossary reads "<label> is the
		// later approved replacement".
		prefix := gloss[:idx]
		fields := strings.FieldsFunc(prefix, func(r rune) bool {
			return !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z')
		})
		// Skip trailing stopwords ("is", "the", "a") to reach the label token.
		for i := len(fields) - 1; i >= 0; i-- {
			w := fields[i]
			switch strings.ToLower(w) {
			case "is", "the", "a", "later", "an", "its":
				continue
			}
			return w
		}
	}
	return ""
}

// aliasEcho returns the entity value (e.g. "dovafin") that the question names
// and that the ledger text repeats as the right-hand side of a "label=value"
// assignment. The alias is the most frequently repeated non-numeric RHS value,
// so labels (which appear once each) are never mistaken for it. Renderer
// delimiters do not matter: the kvWord regex matches label=value anywhere.
func aliasEcho(question, text string) string {
	qWords := map[string]bool{}
	for _, w := range strings.FieldsFunc(question, func(r rune) bool {
		return !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9')
	}) {
		if len(w) >= 5 {
			qWords[w] = true
		}
	}
	counts := map[string]int{}
	for _, m := range kvWord.FindAllStringSubmatch(text, -1) {
		counts[m[2]]++
	}
	best, bestN := "", 0
	for val, n := range counts {
		if qWords[val] && n > bestN {
			best, bestN = val, n
		}
	}
	return best
}

// readLabel returns the value most recently assigned to a label anywhere in the
// (decoy-stripped) evidence text. Last write wins, so a later correction
// supersedes an earlier draft.
func readLabel(text, label string) (int, bool) {
	best, found := 0, false
	for _, m := range kvNum.FindAllStringSubmatch(text, -1) {
		if m[1] == label {
			if v, err := strconv.Atoi(m[2]); err == nil {
				best, found = v, true
			}
		}
	}
	return best, found
}

// robust indicates the more-capable binder: it recovers field roles from the
// composed v11 glossary via loose keyword matching, so it is NOT defeated by the
// glossary rotation alone. What remains is the difficulty from the sampled
// program shapes and descriptive entity binding — the effects that would still
// hurt a materially stronger harness.
var robustApprovedKW = []string{"approv", "replac", "sanction", "supersed"}
var robustPaidKW = []string{"settl", "paid", "payment", "disburs"}

func (e championEngine) answerRobust(question string, evidenceIDs []string) string {
	var gloss, body strings.Builder
	for _, id := range evidenceIDs {
		if p, ok := e.pairs[id]; ok {
			gloss.WriteString(p.Prompt + "\n")
			body.WriteString(decoyStrip.ReplaceAllString(p.Prompt, " ") + "\n")
		}
	}
	text := body.String()
	approvedLabel := bindRoleLoose(gloss.String(), text, robustApprovedKW)
	paidLabel := bindRoleLoose(gloss.String(), text, robustPaidKW)
	if approvedLabel == "" || paidLabel == "" {
		return ""
	}
	if aliasEcho(question, text) == "" {
		return "" // still defeated by descriptive entity binding
	}
	approved, okA := readLabel(text, approvedLabel)
	paid, okP := readLabel(text, paidLabel)
	if !okA || !okP {
		return ""
	}
	return formatMinorUnits(approved - paid) // still the hardcoded v10 program
}

// bindRoleLoose finds the label whose glossary clause contains any role keyword,
// tolerating composed phrasing and interior typos on framing words.
func bindRoleLoose(gloss, text string, keywords []string) string {
	labels := map[string]bool{}
	for _, m := range kvNum.FindAllStringSubmatch(text, -1) {
		labels[m[1]] = true
	}
	for _, m := range kvWord.FindAllStringSubmatch(gloss, -1) {
		labels[m[1]] = true
	}
	lower := strings.ToLower(gloss)
	best, bestIdx := "", 1<<30
	for label := range labels {
		li := strings.Index(lower, strings.ToLower(label))
		if li < 0 {
			continue
		}
		window := lower[li:min(len(lower), li+80)]
		for _, kw := range keywords {
			if ki := strings.Index(window, kw); ki >= 0 && ki < bestIdx {
				best, bestIdx = label, ki
			}
		}
	}
	return best
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

type familyResult struct {
	mean, robustMean float64
	correct, cases   int
}

func evalChampionFamily(t *testing.T, seed int64, version int) familyResult {
	t.Helper()
	prof, ok := gen.ProfileForVersion("full", version)
	if !ok {
		t.Fatalf("no profile for v%d", version)
	}
	artifact, err := gen.GenerateDataset(seed, prof, version)
	if err != nil {
		t.Fatalf("generate v%d seed %d: %v", version, seed, err)
	}
	engine := newChampionEngine(artifact.MemoryWaves)
	sum, robustSum := 0.0, 0.0
	n, correct := 0, 0
	for _, c := range artifact.MemoryCases {
		if c.V10Provenance == nil {
			continue // open-program family only
		}
		mc := c.MemoryCase
		cs := scorer.GradeMemory(mc, protocol.RunResponse{Answer: engine.answer(mc.Question, c.V10EvidencePairIDs)})
		rcs := scorer.GradeMemory(mc, protocol.RunResponse{Answer: engine.answerRobust(mc.Question, c.V10EvidencePairIDs)})
		sum += cs.Score
		robustSum += rcs.Score
		if cs.Correct {
			correct++
		}
		n++
	}
	res := familyResult{correct: correct, cases: n}
	if n > 0 {
		res.mean = sum / float64(n)
		res.robustMean = robustSum / float64(n)
	}
	return res
}

func TestV11ChampionStrategyCollapse(t *testing.T) {
	seeds := []int64{}
	for s := int64(1001); s <= 1012; s++ {
		seeds = append(seeds, s)
	}
	var v10Means, v11Means, v10Rob, v11Rob []float64
	fmt.Println("            v10-fitted rule engine        robust binder (composed-glossary-proof)")
	fmt.Println("seed        v10_mean   v11_mean           v10_mean   v11_mean")
	for _, seed := range seeds {
		r10 := evalChampionFamily(t, seed, protocol.BenchVersionV10)
		r11 := evalChampionFamily(t, seed, protocol.BenchVersionV11)
		v10Means = append(v10Means, r10.mean)
		v11Means = append(v11Means, r11.mean)
		v10Rob = append(v10Rob, r10.robustMean)
		v11Rob = append(v11Rob, r11.robustMean)
		fmt.Printf("%-10d  %8.4f   %8.4f           %8.4f   %8.4f\n",
			seed, r10.mean, r11.mean, r10.robustMean, r11.robustMean)
	}
	fmt.Printf("\nMEAN        %8.4f   %8.4f           %8.4f   %8.4f\n",
		mean(v10Means), mean(v11Means), mean(v10Rob), mean(v11Rob))
	fmt.Printf("median      %8.4f   %8.4f           %8.4f   %8.4f\n",
		median(v10Means), median(v11Means), median(v10Rob), median(v11Rob))

	// The v10-fitted rule engine must reproduce competence on v10 and collapse
	// on v11; even the composed-glossary-proof robust binder must fall hard.
	if mean(v10Means) < 0.80 {
		t.Fatalf("champion emulator did not reproduce v10 competence: %.4f", mean(v10Means))
	}
	if mean(v11Means) >= mean(v10Means)-0.50 {
		t.Fatalf("v11 did not degrade the v10-fitted strategy enough: v10=%.4f v11=%.4f", mean(v10Means), mean(v11Means))
	}
	if mean(v11Rob) >= mean(v10Rob)-0.30 {
		t.Fatalf("v11 did not degrade the robust binder from program-shape/binding alone: v10=%.4f v11=%.4f", mean(v10Rob), mean(v11Rob))
	}
}

func mean(xs []float64) float64 {
	s := 0.0
	for _, x := range xs {
		s += x
	}
	if len(xs) == 0 {
		return 0
	}
	return s / float64(len(xs))
}

func median(xs []float64) float64 {
	c := append([]float64(nil), xs...)
	sort.Float64s(c)
	if len(c) == 0 {
		return 0
	}
	if len(c)%2 == 1 {
		return c[len(c)/2]
	}
	return (c[len(c)/2-1] + c[len(c)/2]) / 2
}
