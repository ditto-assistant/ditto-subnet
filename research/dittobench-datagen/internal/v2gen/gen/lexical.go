package gen

import (
	"regexp"
	"strings"
)

// wordRe matches a lowercase alphanumeric token; isNumeric reports whether a
// token is a pure digit run. Generic text utilities used by the overlap
// measurement below.
var wordRe = regexp.MustCompile(`[a-z0-9]+`)

var numberRe = regexp.MustCompile(`[0-9]+`)

func isNumeric(s string) bool { return numberRe.MatchString(s) && wordRe.FindString(s) == s }

// Lexical-overlap measurement for the query↔needle gap.
//
// NoLiMa (arXiv:2502.05167) shows that when a benchmark question shares content
// words with the stored fact ("What is my favorite COLOR?" ↔ "my favorite COLOR
// is teal"), a memory system can retrieve by lexical shortcut and its measured
// ability is massively overstated: restoring the literal match restores the
// score. We reduce that by rewording questions to share fewer content words with
// their evidence (see question_gap.go) and by MEASURING the residual overlap as
// advisory telemetry so the gap is visible, never silently assumed closed.

// overlapStopwords are dropped before measuring content overlap: question
// scaffolding ("what/which/do/my/currently"), articles, and generic verbs. What
// remains is the concrete content (the attribute noun + any entity), which is
// exactly what a lexical retriever keys on.
var overlapStopwords = map[string]bool{
	"what": true, "which": true, "who": true, "whom": true, "whose": true,
	"where": true, "when": true, "how": true, "why": true, "do": true,
	"does": true, "did": true, "is": true, "are": true, "am": true, "was": true,
	"were": true, "be": true, "been": true, "have": true, "has": true, "had": true,
	"my": true, "me": true, "i": true, "you": true, "your": true, "the": true,
	"a": true, "an": true, "of": true, "to": true, "for": true, "on": true,
	"in": true, "at": true, "by": true, "with": true, "about": true, "and": true,
	"or": true, "that": true, "this": true, "these": true, "those": true,
	"it": true, "its": true, "as": true, "so": true, "if": true, "any": true,
	"all": true, "many": true, "much": true, "some": true, "more": true,
	"tell": true, "told": true, "mentioned": true, "said": true, "say": true,
	"currently": true, "current": true, "now": true, "days": true,
	"different": true, "separate": true, "please": true, "would": true,
	"could": true, "can": true, "should": true, "will": true, "just": true,
	"really": true, "ever": true, "still": true, "there": true, "here": true,
	"list": true, "name": true, "names": true, "give": true, "provide": true,
	"suggest": true, "want": true, "like": true, "think": true, "feel": true,
}

// contentTokens returns the set of content words in s (lowercased `[a-z0-9]+`
// tokens ≥3 chars or numeric, minus overlapStopwords).
func contentTokens(s string) map[string]bool {
	out := map[string]bool{}
	for _, tok := range wordRe.FindAllString(strings.ToLower(s), -1) {
		if overlapStopwords[tok] {
			continue
		}
		if len(tok) < 3 && !isNumeric(tok) {
			continue
		}
		out[tok] = true
	}
	return out
}

// sharedContentCount is the number of content words the question and evidence
// share: the ABSOLUTE strength of the lexical-retrieval shortcut. Unlike the
// coefficient below it cannot be gamed by padding the question with filler
// (which lowers a ratio without removing the matchable term), so it is the
// acceptance criterion for a low-overlap rewrite: a genuine synonym swap must
// drop a shared word.
func sharedContentCount(question, evidence string) int {
	q := contentTokens(question)
	e := contentTokens(evidence)
	shared := 0
	for tok := range q {
		if e[tok] {
			shared++
		}
	}
	return shared
}

// lexicalOverlap is the fraction of the QUESTION's content words that also appear
// in the evidence text: the interpretable [0,1] telemetry figure a lexical
// retriever exploits. Lower means a harder, more memory-testing question. An
// empty question or empty evidence yields 0.
func lexicalOverlap(question, evidence string) float64 {
	q := contentTokens(question)
	if len(q) == 0 {
		return 0
	}
	if len(contentTokens(evidence)) == 0 {
		return 0
	}
	return float64(sharedContentCount(question, evidence)) / float64(len(q))
}
