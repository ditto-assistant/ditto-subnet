package parserprobe

import (
	"fmt"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Options configures one probe run.
type Options struct {
	BenchVersion int
	RunSize      string
	FirstSeed    int64
	Seeds        int
	// RouterSeeds trains the N14 router on this many seeds starting at
	// RouterFirstSeed (disjoint from the probe seeds). 0 disables the router.
	RouterSeeds     int
	RouterFirstSeed int64
	// Artifacts, when set, are probed instead of generating seeds (the
	// surface-passed artifact path).
	Artifacts []gen.DatasetArtifact
}

// Stats is one slice's outcome under one adversary.
type Stats struct {
	Cases        int     `json:"cases"`
	FamilyID     int     `json:"family_id"`
	Answered     int     `json:"answered"`
	ScoreSum     float64 `json:"score_sum"`
	FamilyIDRate float64 `json:"family_id_rate"`
	AnswerRate   float64 `json:"answer_rate"`
	Mean         float64 `json:"mean"`
}

func (s *Stats) add(familyOK, answered bool, score float64) {
	s.Cases++
	if familyOK {
		s.FamilyID++
	}
	if answered {
		s.Answered++
	}
	s.ScoreSum += score
	s.finish()
}

func (s *Stats) merge(o Stats) {
	s.Cases += o.Cases
	s.FamilyID += o.FamilyID
	s.Answered += o.Answered
	s.ScoreSum += o.ScoreSum
	s.finish()
}

func (s *Stats) finish() {
	if s.Cases == 0 {
		return
	}
	s.FamilyIDRate = float64(s.FamilyID) / float64(s.Cases)
	s.AnswerRate = float64(s.Answered) / float64(s.Cases)
	s.Mean = s.ScoreSum / float64(s.Cases)
}

// Variant is one adversary's full result on one artifact.
type Variant struct {
	Memory   Stats            `json:"memory"`
	Tool     Stats            `json:"tool"`
	Slices   map[string]Stats `json:"slices"`
	Families map[string]Stats `json:"families"`
	// Composite mirrors the scorer's 0.5*tool_mean + 0.5*memory_mean before
	// run-level factors.
	Composite    float64 `json:"composite"`
	FamilyIDRate float64 `json:"family_id_rate"`
	AnswerRate   float64 `json:"answer_rate"`
}

func newVariant() *Variant {
	return &Variant{Slices: map[string]Stats{}, Families: map[string]Stats{}}
}

func (v *Variant) record(side, slice, family string, familyOK, answered bool, score float64) {
	if side == "memory" {
		v.Memory.add(familyOK, answered, score)
	} else {
		v.Tool.add(familyOK, answered, score)
	}
	s := v.Slices[slice]
	s.add(familyOK, answered, score)
	v.Slices[slice] = s
	f := v.Families[family]
	f.add(familyOK, answered, score)
	v.Families[family] = f
}

func (v *Variant) finish() {
	v.Composite = 0.5*v.Tool.Mean + 0.5*v.Memory.Mean
	total := v.Memory.Cases + v.Tool.Cases
	if total > 0 {
		v.FamilyIDRate = float64(v.Memory.FamilyID+v.Tool.FamilyID) / float64(total)
		v.AnswerRate = float64(v.Memory.Answered+v.Tool.Answered) / float64(total)
	}
}

func (v *Variant) merge(o *Variant) {
	v.Memory.merge(o.Memory)
	v.Tool.merge(o.Tool)
	for k, s := range o.Slices {
		cur := v.Slices[k]
		cur.merge(s)
		v.Slices[k] = cur
	}
	for k, s := range o.Families {
		cur := v.Families[k]
		cur.merge(s)
		v.Families[k] = cur
	}
	v.finish()
}

// SeedReport is one artifact's result under both adversaries.
type SeedReport struct {
	Seed           int64    `json:"seed"`
	BenchVersion   int      `json:"bench_version"`
	MemoryCases    int      `json:"memory_cases"`
	ToolCases      int      `json:"tool_cases"`
	GIH            *Variant `json:"gih"`
	Router         *Variant `json:"router,omitempty"`
	FamilyIDRate   float64  `json:"family_id_rate"`
	MemoryFamilyID float64  `json:"memory_family_id_rate"`
	AnswerRate     float64  `json:"answer_rate"`
	Composite      float64  `json:"composite"`
}

// Report is the whole run.
type Report struct {
	BenchVersion    int          `json:"bench_version"`
	RunSize         string       `json:"run_size"`
	Source          string       `json:"source"`
	RouterSeeds     int          `json:"router_seeds"`
	Seeds           []SeedReport `json:"seeds"`
	GIH             *Variant     `json:"gih_aggregate"`
	Router          *Variant     `json:"router_aggregate,omitempty"`
	CompositeMin    float64      `json:"gih_composite_min"`
	CompositeMax    float64      `json:"gih_composite_max"`
	Unclassified    []string     `json:"unclassified_families,omitempty"`
	UnmatchedSample []string     `json:"unmatched_sample,omitempty"`
}

// Run executes the probe over generated seeds or supplied artifacts.
func Run(opts Options) (Report, error) {
	if opts.RunSize == "" {
		opts.RunSize = "full"
	}
	if opts.Seeds <= 0 && len(opts.Artifacts) == 0 {
		return Report{}, fmt.Errorf("at least one seed or artifact is required")
	}
	report := Report{BenchVersion: opts.BenchVersion, RunSize: opts.RunSize, Source: "generated", GIH: newVariant(), RouterSeeds: opts.RouterSeeds}
	var rt *router
	if opts.RouterSeeds > 0 {
		first := opts.RouterFirstSeed
		if first == 0 {
			first = opts.FirstSeed + int64(opts.Seeds) + 1000
		}
		var err error
		rt, err = trainRouter(opts.BenchVersion, opts.RunSize, first, opts.RouterSeeds)
		if err != nil {
			return Report{}, fmt.Errorf("train router: %w", err)
		}
		report.Router = newVariant()
	}
	artifacts := opts.Artifacts
	if len(artifacts) == 0 {
		prof, ok := gen.ProfileForVersion(opts.RunSize, opts.BenchVersion)
		if !ok {
			return Report{}, fmt.Errorf("unsupported run size %q for bench v%d", opts.RunSize, opts.BenchVersion)
		}
		for i := 0; i < opts.Seeds; i++ {
			a, err := gen.GenerateDataset(opts.FirstSeed+int64(i), prof, opts.BenchVersion)
			if err != nil {
				return Report{}, err
			}
			artifacts = append(artifacts, a)
		}
	} else {
		report.Source = "artifact"
	}
	unclassified := map[string]bool{}
	for i, a := range artifacts {
		sr, unmatched, missing := Probe(a, rt)
		for f := range missing {
			unclassified[f] = true
		}
		if len(report.UnmatchedSample) < 12 {
			report.UnmatchedSample = append(report.UnmatchedSample, unmatched...)
			if len(report.UnmatchedSample) > 12 {
				report.UnmatchedSample = report.UnmatchedSample[:12]
			}
		}
		report.Seeds = append(report.Seeds, sr)
		report.GIH.merge(sr.GIH)
		if rt != nil && sr.Router != nil {
			report.Router.merge(sr.Router)
		}
		if i == 0 || sr.Composite < report.CompositeMin {
			report.CompositeMin = sr.Composite
		}
		if i == 0 || sr.Composite > report.CompositeMax {
			report.CompositeMax = sr.Composite
		}
	}
	for f := range unclassified {
		report.Unclassified = append(report.Unclassified, f)
	}
	sort.Strings(report.Unclassified)
	return report, nil
}

// Probe evaluates both adversaries on one artifact. It returns the report, a
// small sample of questions no frame matched, and the set of artifact
// families the frame banks do not know.
func Probe(a gen.DatasetArtifact, rt *router) (SeedReport, []string, map[string]bool) {
	stores := buildStores(a)
	tp := newToolParser(a.BenchVersion)
	needles := fixtureNeedles(a)
	sr := SeedReport{Seed: a.Seed, BenchVersion: a.BenchVersion, MemoryCases: len(a.MemoryCases), ToolCases: len(a.ToolCases), GIH: newVariant()}
	if rt != nil {
		sr.Router = newVariant()
	}
	var unmatched []string
	missing := map[string]bool{}
	known := knownFamilies(a.BenchVersion)
	// Per-user, per-family ordinals for the families whose question repeats
	// verbatim within a run (see answerMemory).
	ordinals := map[string]int{}
	routerOrdinals := map[string]int{}
	next := func(m map[string]int, user, family string) int {
		key := user + "\x00" + family
		n := m[key]
		m[key] = n + 1
		return n
	}
	for _, ac := range a.MemoryCases {
		user := ac.UserID
		if user == "" {
			user = gen.PrimaryUser
		}
		st := stores[user]
		if st == nil {
			st = newStore(a.BenchVersion)
		}
		if !known[ac.QuestionType] {
			missing[ac.QuestionType] = true
		}
		slice := memorySlice(ac.MemoryCase)
		// GIH: frame banks decide the family.
		pq, ok := classifyMemory(a.BenchVersion, ac.Question)
		var d derived
		if ok {
			d = answerMemory(st, pq, next(ordinals, user, pq.family))
		} else if len(unmatched) < 6 {
			unmatched = append(unmatched, ac.QuestionType+": "+ac.Question)
		}
		score := 0.0
		if d.ok {
			score = grade.Memory(ac.MemoryCase, launder(d)).Score
		}
		sr.GIH.record("memory", slice, ac.QuestionType, familyMatches(d.family, pq.family, ok, ac.QuestionType), d.ok, score)

		if rt != nil {
			// Router: the classifier names the family, the family's own frames
			// extract the slots.
			fam := rt.predict("mem:", ac.Question)
			rq, rok := classifyWithin(a.BenchVersion, fam, ac.Question)
			var rd derived
			if rok {
				rd = answerMemory(st, rq, next(routerOrdinals, user, rq.family))
			}
			rscore := 0.0
			if rd.ok {
				rscore = grade.Memory(ac.MemoryCase, launder(rd)).Score
			}
			sr.Router.record("memory", slice, ac.QuestionType, familyMatches(rd.family, fam, rok, ac.QuestionType), rd.ok, rscore)
		}
	}
	primary := stores[gen.PrimaryUser]
	fetchOrdinal := 0
	for _, tc := range a.ToolCases {
		excluded := resultUsageArgs(tc.Category, needleValue(needles[tc.ID]))
		want := toolSignature(tc.ExpectedTools, tc.FuzzyTrajectory, excluded)
		pred := predictTool(tp, tc.Prompt, primary, &fetchOrdinal)
		got := toolSignature(pred.tools, tc.FuzzyTrajectory, excluded)
		score := 0.0
		if pred.ok && got == want {
			score = 1
		}
		sr.GIH.record("tool", "tool-prompts", tc.Category, pred.ok && pred.category == tc.Category, pred.ok, score)
		if rt != nil {
			fam := rt.predict("tool:", tc.Prompt)
			rscore := 0.0
			// The router only names the category; the outcome still needs the
			// frame parse for slots, so it is credited when the frame parse agrees.
			if pred.ok && pred.category == fam && got == want {
				rscore = 1
			}
			sr.Router.record("tool", "tool-prompts", tc.Category, fam == tc.Category, pred.ok && pred.category == fam, rscore)
		}
	}
	sr.GIH.finish()
	sr.FamilyIDRate, sr.MemoryFamilyID, sr.AnswerRate, sr.Composite = sr.GIH.FamilyIDRate, sr.GIH.Memory.FamilyIDRate, sr.GIH.AnswerRate, sr.GIH.Composite
	if sr.Router != nil {
		sr.Router.finish()
	}
	return sr, unmatched, missing
}

// resultUsageArgs names the argument values a parser cannot know without
// executing the served tool: the fixture needle value, and for result-usage
// families the gmail_send body that must carry it. Any harness, honest or
// not, obtains them by calling the tool, so they are excluded from the
// model-free outcome comparison on both sides.
func resultUsageArgs(category, needle string) func(key, value string) bool {
	resultUsage := strings.HasSuffix(category, "_result_usage")
	return func(key, value string) bool {
		if needle != "" && value == needle {
			return true
		}
		return resultUsage && key == "body"
	}
}

func predictTool(tp *toolParser, prompt string, st *store, fetchOrdinal *int) toolPrediction {
	if _, _, ok := matchAny(memoryFetchFrames, strings.TrimSpace(prompt)); ok {
		n := *fetchOrdinal
		*fetchOrdinal = n + 1
		return memoryFetchPrediction(st, n)
	}
	return tp.classifyTool(prompt, st)
}

// familyMatches credits family identification when the parser's final verdict
// (record-resolved where applicable) equals the artifact family.
func familyMatches(resolved, surface string, ok bool, want string) bool {
	if !ok {
		return false
	}
	if resolved != "" && resolved == want {
		return true
	}
	return surface == want
}

// classifyWithin matches the question only against the frames of one family.
func classifyWithin(benchVersion int, family, question string) (parsedQuestion, bool) {
	q := strings.TrimSpace(question)
	if isProgramFamily(family) {
		return classifyProgramQuestion(benchVersion, q)
	}
	for _, fam := range memoryFamilies {
		if fam.name != family {
			continue
		}
		if i, slots, ok := matchAny(fam.frames, q); ok {
			return parsedQuestion{family: fam.name, slots: slots, frame: i}, true
		}
	}
	return parsedQuestion{}, false
}

// knownFamilies is the set of artifact families the frame banks can invert for
// benchVersion. The program family is known only when its contract's grammar is
// registered (programGrammars), so a re-rendered program surface surfaces in
// Report.Unclassified instead of scoring a silent zero.
func knownFamilies(benchVersion int) map[string]bool {
	out := map[string]bool{}
	if _, ok := programGrammarFor(benchVersion); ok {
		out[programFamily(benchVersion)] = true
	}
	for _, fam := range memoryFamilies {
		out[fam.name] = true
	}
	for _, shape := range []string{"plain", "adjusted", "superseded", "capped", "forgiven", "referred", "cf-base", "cf-variant"} {
		out["record-balance-"+shape] = true
	}
	return out
}

// memorySlice names the surface slice a memory case belongs to.
func memorySlice(mc protocol.MemoryCase) string {
	qt := mc.QuestionType
	switch {
	case strings.HasPrefix(qt, "world-story-"):
		return "story"
	case strings.Contains(qt, "open-program") || strings.HasPrefix(qt, "record-balance-"):
		return "programs"
	case strings.HasPrefix(qt, "world-project-"):
		return "business"
	case strings.HasPrefix(qt, "conversational-") || strings.HasPrefix(qt, "declarative-") || qt == "world-canary" || strings.Contains(qt, "injection"):
		return "integrity"
	case mc.AnswerKind == protocol.AnswerNumber:
		return "quantity"
	default:
		return "personal"
	}
}

// launder is the single "reply exactly" completion of the compute-then-launder
// archetype: the derived value is rendered as a natural assistant reply and
// placed in the authoritative answer slot. Money is spoken in decimal currency
// (the grader rejects raw minor units), lists are joined with "; ".
func launder(d derived) protocol.RunResponse {
	switch d.kind {
	case protocol.AnswerChitchat:
		return protocol.RunResponse{FinalText: "Hey! Doing well, thanks for asking — how is your day going?"}
	case protocol.AnswerDecline:
		return protocol.RunResponse{Abstain: true, FinalText: "I don't have that on record."}
	case protocol.AnswerAcknowledge:
		return protocol.RunResponse{FinalText: "Done — noted.", Answer: "noted"}
	case protocol.AnswerList, protocol.AnswerOrderedList:
		parts := make([]string, len(d.items))
		for i, item := range d.items {
			kind := protocol.AnswerValue
			if i < len(d.kinds) {
				kind = d.kinds[i]
			}
			parts[i] = renderScalar(kind, item)
		}
		answer := strings.Join(parts, "; ")
		return protocol.RunResponse{FinalText: "Here is what I have: " + answer + ".", Answer: answer}
	}
	answer := renderScalar(d.kind, d.value)
	return protocol.RunResponse{FinalText: "The answer is " + answer + ".", Answer: answer}
}

func renderScalar(kind, value string) string {
	if kind == protocol.AnswerMoney {
		return formatMoney(atoi(value))
	}
	return value
}

// formatMoney renders integer cents as $1,234.56.
func formatMoney(cents int) string {
	neg := cents < 0
	if neg {
		cents = -cents
	}
	whole := cents / 100
	digits := fmt.Sprint(whole)
	var b strings.Builder
	for i, r := range digits {
		if i > 0 && (len(digits)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(r)
	}
	out := fmt.Sprintf("$%s.%02d", b.String(), cents%100)
	if neg {
		return "-" + out
	}
	return out
}
