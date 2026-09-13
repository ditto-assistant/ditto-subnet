package parserprobe

import (
	"math"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
)

// router is the N14 adversary: a multinomial naive-Bayes family classifier
// trained on locally generated seeds. It replaces only the family-identification
// step of the generator-inverse harness — slots, oracles, and the launder step
// are shared — so the two adversaries differ exactly in how they recognise a
// question, which is what a private surface pass changes.
//
// Features are lowercase word tokens plus character 4-grams, so a typo moves a
// handful of grams rather than deleting a whole feature. Training on 10,000 full
// seeds is the documented N14 configuration; CI trains on a small held-out
// disjoint range because the classifier converges within a few hundred seeds.
type router struct {
	classes   []string
	classLogP map[string]float64
	featLogP  map[string]map[string]float64 // class -> feature -> log P(f|c)
	unseen    map[string]float64            // class -> log P(unseen feature|c)
	trained   int
}

func newRouter() *router {
	return &router{classLogP: map[string]float64{}, featLogP: map[string]map[string]float64{}, unseen: map[string]float64{}}
}

// trainRouter generates seeds [first, first+n) and fits the memory-question
// and tool-prompt classifiers. Tool categories and memory families share one
// label space prefixed by side so the two never collide.
func trainRouter(benchVersion int, runSize string, first int64, n int) (*router, error) {
	prof, ok := gen.ProfileForVersion(runSize, benchVersion)
	if !ok {
		return nil, errUnsupportedRunSize(runSize)
	}
	counts := map[string]map[string]int{}
	classN := map[string]int{}
	total := 0
	add := func(class, text string) {
		m, ok := counts[class]
		if !ok {
			m = map[string]int{}
			counts[class] = m
		}
		for _, f := range routerFeatures(text) {
			m[f]++
		}
		classN[class]++
		total++
	}
	for i := 0; i < n; i++ {
		a, err := gen.GenerateDataset(first+int64(i), prof, benchVersion)
		if err != nil {
			return nil, err
		}
		for _, mc := range a.MemoryCases {
			add("mem:"+routerLabel(mc.QuestionType), mc.Question)
		}
		for _, tc := range a.ToolCases {
			add("tool:"+tc.Category, tc.Prompt)
		}
	}
	r := newRouter()
	r.trained = n
	vocab := map[string]bool{}
	for _, m := range counts {
		for f := range m {
			vocab[f] = true
		}
	}
	v := float64(len(vocab))
	for class, m := range counts {
		r.classes = append(r.classes, class)
		r.classLogP[class] = math.Log(float64(classN[class]) / float64(total))
		sum := 0
		for _, c := range m {
			sum += c
		}
		denom := float64(sum) + v // Laplace smoothing
		fl := make(map[string]float64, len(m))
		for f, c := range m {
			fl[f] = math.Log((float64(c) + 1) / denom)
		}
		r.featLogP[class] = fl
		r.unseen[class] = math.Log(1 / denom)
	}
	sort.Strings(r.classes)
	return r, nil
}

// routerLabel collapses the record-balance shapes to one surface label: the
// question is identical across shapes by construction, so the classifier can
// only recover the family, and the record parser resolves the shape.
func routerLabel(questionType string) string {
	if strings.HasPrefix(questionType, "record-balance-") {
		return "record-balance"
	}
	return questionType
}

func routerFeatures(text string) []string {
	lower := strings.ToLower(text)
	var out []string
	for _, w := range strings.FieldsFunc(lower, func(r rune) bool {
		return !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '\'')
	}) {
		if len(w) >= 2 {
			out = append(out, "w:"+w)
		}
	}
	runes := []rune(" " + strings.Join(strings.Fields(lower), " ") + " ")
	for i := 0; i+4 <= len(runes); i++ {
		out = append(out, "g:"+string(runes[i:i+4]))
	}
	return out
}

// predict returns the best class with the given side prefix ("mem:" / "tool:").
func (r *router) predict(side, text string) string {
	if r == nil || len(r.classes) == 0 {
		return ""
	}
	feats := routerFeatures(text)
	best, bestScore := "", math.Inf(-1)
	for _, class := range r.classes {
		if !strings.HasPrefix(class, side) {
			continue
		}
		score := r.classLogP[class]
		fl := r.featLogP[class]
		unseen := r.unseen[class]
		for _, f := range feats {
			if p, ok := fl[f]; ok {
				score += p
			} else {
				score += unseen
			}
		}
		if score > bestScore {
			best, bestScore = class, score
		}
	}
	return strings.TrimPrefix(best, side)
}

type errUnsupportedRunSize string

func (e errUnsupportedRunSize) Error() string { return "unsupported run size " + string(e) }
