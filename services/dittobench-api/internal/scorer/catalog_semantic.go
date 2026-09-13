package scorer

import (
	"math"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The Bench v13 semantic-preloading safe harbor needs a PUBLISHED, model-free,
// deterministic ranking of the catalog against a request so that "the retained
// set contains the semantic top-k" is a fact anyone can recompute from the
// dataset and the transcript. This file is that ranking: a lexical embedding
// over each tool's name and description (TF-IDF over the offered catalog,
// cosine similarity to the request), stdlib only, no model, no randomness.
//
// It is deliberately simple. It is not the harness's preloader and does not
// need to agree with one; it is the reference the safe harbor is stated
// against, so a preloader that keeps at least these k tools is never zeroed
// for trimming. k is CatalogSafeHarborTopK.

// CatalogSafeHarborTopK is the k of the published semantic-preloading safe
// harbor: a trimmed catalog that still contains the top-k tools of the
// published embedding for the request is never charged for the trim.
const CatalogSafeHarborTopK = 3

// catalogStopwords are dropped from both sides before scoring. Short function
// words carry no tool semantics and would otherwise dominate a chit-chat prompt.
var catalogStopwords = map[string]struct{}{
	"the": {}, "and": {}, "for": {}, "with": {}, "that": {}, "this": {}, "from": {},
	"you": {}, "your": {}, "can": {}, "please": {}, "one": {}, "more": {}, "use": {},
	"are": {}, "was": {}, "were": {}, "have": {}, "has": {}, "had": {}, "not": {},
	"but": {}, "any": {}, "all": {}, "into": {}, "its": {}, "our": {}, "out": {},
	"about": {}, "what": {}, "which": {}, "when": {}, "where": {}, "who": {},
	"how": {}, "why": {}, "just": {}, "like": {}, "then": {}, "than": {}, "too": {},
	"very": {}, "will": {}, "would": {}, "should": {}, "could": {}, "there": {},
	"here": {}, "some": {}, "them": {}, "they": {}, "their": {}, "thing": {},
	"tool": {}, "tools": {}, "given": {}, "return": {}, "returns": {}, "default": {},
}

// catalogStem applies a light, fixed suffix strip so "searching", "searched",
// "searches", and "search" collapse together. It is intentionally crude and
// applied identically to both sides.
func catalogStem(token string) string {
	for _, suffix := range []string{"ing", "ed", "es", "s"} {
		if len(token) > len(suffix)+3 && strings.HasSuffix(token, suffix) {
			return token[:len(token)-len(suffix)]
		}
	}
	return token
}

// catalogTokens lowercases text, splits on every non-alphanumeric byte (so
// snake_case tool names split into their words), drops short tokens and
// stopwords, and stems. Returns the distinct token set.
func catalogTokens(text string) map[string]struct{} {
	tokens := make(map[string]struct{})
	var current strings.Builder
	flush := func() {
		if current.Len() == 0 {
			return
		}
		token := current.String()
		current.Reset()
		if len(token) < 3 {
			return
		}
		if _, stop := catalogStopwords[token]; stop {
			return
		}
		tokens[catalogStem(token)] = struct{}{}
	}
	for _, r := range strings.ToLower(text) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			current.WriteRune(r)
		default:
			flush()
		}
	}
	flush()
	return tokens
}

// CatalogSemanticTopK ranks catalog against prompt under the published lexical
// embedding and returns the names of the top k tools (fewer when the catalog is
// smaller). Ties break on tool name so the result is a pure function of its
// inputs. Tools that share no token with the prompt score 0 but are still
// ranked, so k names are always returned for a catalog of at least k tools.
func CatalogSemanticTopK(prompt string, catalog []protocol.ToolDefinition, k int) []string {
	if k <= 0 || len(catalog) == 0 {
		return nil
	}
	// Document frequency over the catalog: a token every tool mentions ("user")
	// carries no discrimination.
	docs := make([]map[string]struct{}, len(catalog))
	df := make(map[string]int)
	for index, tool := range catalog {
		docs[index] = catalogTokens(tool.Name + " " + tool.Description)
		for token := range docs[index] {
			df[token]++
		}
	}
	idf := func(token string) float64 {
		return math.Log(1 + float64(len(catalog))/float64(1+df[token]))
	}
	query := catalogTokens(prompt)
	queryNorm := 0.0
	for token := range query {
		w := idf(token)
		queryNorm += w * w
	}
	type ranked struct {
		name  string
		score float64
	}
	scores := make([]ranked, 0, len(catalog))
	for index, tool := range catalog {
		dot, docNorm := 0.0, 0.0
		for token := range docs[index] {
			w := idf(token)
			docNorm += w * w
			if _, shared := query[token]; shared {
				dot += w * w
			}
		}
		score := 0.0
		if dot > 0 && docNorm > 0 && queryNorm > 0 {
			score = dot / (math.Sqrt(docNorm) * math.Sqrt(queryNorm))
		}
		scores = append(scores, ranked{name: tool.Name, score: score})
	}
	sort.SliceStable(scores, func(i, j int) bool {
		if scores[i].score != scores[j].score {
			return scores[i].score > scores[j].score
		}
		return scores[i].name < scores[j].name
	})
	if k > len(scores) {
		k = len(scores)
	}
	names := make([]string, 0, k)
	for _, entry := range scores[:k] {
		names = append(names, entry.name)
	}
	return names
}
