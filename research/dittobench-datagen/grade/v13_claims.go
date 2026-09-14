package grade

import (
	"sort"
	"strconv"
	"strings"
	"sync"

	"github.com/ditto-assistant/dittobench-datagen/internal/multilingual"
)

// v13 claim engine. It turns a free-form reply into a set of ASSERTED
// candidates for one typed claim, so the typed matcher can grade what the
// reply claims rather than what it merely mentions. The rules are deliberately
// structural (sentence and clause markers), never semantic, so any third party
// reproduces the segmentation exactly:
//
//   - a sentence ending in "?" (or opened with "¿") restates a question and
//     asserts nothing; a clause carrying an echo marker ("whether", "you asked")
//     is treated the same way;
//   - a span opened by a rejection connective ("not", "rather than", "isn't")
//     up to the next clause boundary is REJECTED: values inside it are cited,
//     not claimed ("Lisbon, not Oslo"; "I have $500 for March, not April");
//   - a clause carrying a strong past marker ("used to", "previously", "at
//     first", "I first thought") is SUPERSEDED: its values are the old state,
//     so "I first thought Oslo, but it is Lisbon" asserts only Lisbon;
//   - a clause carrying only a weak past marker ("was", "were") is superseded
//     only when another clause asserts a current value of the same kind, so a
//     past-tense question can still be answered in the past tense ("the
//     balance was $3,800") while "was X, now Y" counts once, as Y;
//   - a sentence opened by a correction marker ("Actually, ...") rejects the
//     sentence before it, so honest self-correction is not a contradiction;
//   - inside a sentence, a value preceded (or followed) by a claim cue ("is",
//     "=", "leaves", "remaining", "total") is the claimed result; when a
//     sentence has cue-positioned values, only those are candidates, so shown
//     arithmetic ("$5,000 minus $1,200 leaves $3,800") asserts one amount;
//   - a clause that enumerates values ("3800 or 4200", "either A or B",
//     "maybe X") asserts every value it lists — a hedge is a claim of each;
//   - a sentence with several values and neither a cue nor an enumeration is
//     exposition and asserts nothing; a lone value in a sentence is asserted;
//   - two refinements keep natural concise answers out of the exposition rule:
//     a calendar-year token beside a count or amount ("3 trips in 2026") is a
//     qualifier the typed specs drop (yearLikeV13), and an uncued bare integer
//     beside an explicitly marked amount ("$3,800 across 4 trips") is
//     exposition while the marked amount is the claim.
//
// The engine works on the folded text of final_text (foldV13) and treats the
// structured answer slot, when populated, as an unconditional assertion.

// claimLexicon is the English structural vocabulary merged with the lexicon
// of the case's question language (multilingual.For). Every list is folded.
type claimLexicon struct {
	rejection, contrast, boundary []string
	pastStrong, pastWeak, current []string
	correction, hedge, enumerator []string
	claimCueStrong, claimCueWeak  []string
	claimCueAfter                 []string
	citation                      []string
	echo, clarify                 []string
	protected                     []string
	decline, acknowledge          []string
	increase, decrease, unchanged []string
	neither                       []string
	numberWords                   map[string]int
	months                        map[string]int
	minorUnit, majorUnit          []string
}

var (
	rejectionEN = []string{
		"not", "never", "no", "isn't", "is not", "wasn't", "was not", "aren't", "are not",
		"weren't", "were not", "rather than", "instead of", "nor", "neither", "without",
		"don't", "do not", "doesn't", "does not", "didn't", "did not", "can't", "cannot",
		"won't", "wouldn't", "hasn't", "haven't", "shouldn't", "isnt", "wasnt", "dont",
		"doesnt", "didnt", "except", "excluding", "other than", "as opposed to", "unlike",
	}
	contrastEN = []string{
		"but", "however", "whereas", "although", "though", "yet", "while", "instead",
		"on the other hand", "then again", "and",
	}
	pastStrongEN = []string{
		"used to", "previously", "formerly", "before that", "before this", "before the", "before",
		"earlier", "originally", "initially", "at first", "first thought", "in the past",
		"back then", "old value", "older value", "prior to", "the old", "the previous",
		"the earlier", "the original", "outdated", "superseded", "obsolete", "once was",
		"had been", "at the time", "no longer",
	}
	pastWeakEN = []string{"was", "were", "had"}
	currentEN  = []string{
		"now", "currently", "today", "at present", "as of now", "as of today", "latest",
		"current", "presently", "these days", "nowadays", "at the moment", "right now",
		"up to date", "most recent", "the new", "newer", "updated", "revised", "final",
	}
	correctionEN = []string{
		"actually", "correction", "wait", "sorry", "scratch that", "let me correct",
		"i mean", "my mistake", "to be precise", "on reflection", "on second thought",
		"correcting myself", "i misspoke", "strike that", "rather", "no", "hmm", "oops",
	}
	hedgeEN = []string{
		"either", "maybe", "perhaps", "possibly", "could be", "might be", "may be", "one of",
		"alternatively", "candidates", "options", "possibilities", "or maybe", "or possibly",
		"not sure which", "unsure which", "any of",
	}
	enumeratorEN = []string{"or", "/", "and/or", "&"}
	// claimCueStrongEN name the RESULT of a computation or the asked attribute;
	// when a sentence carries one, only the values it cues are asserted.
	claimCueStrongEN = []string{
		"=", "equals", "equal to", "comes to", "come to", "totals", "total", "total of",
		"totaling", "totalling", "leaves", "leaving", "left with", "gives", "giving", "yields",
		"so", "therefore", "hence", "thus", "result", "answer", "balance", "net", "makes",
		"making", "arrive at", "arriving at", "amounts to", "amount to", "sits at", "stands at",
		"landed at", "lands at", "ends at", "ended at", "→", "->", ":", "which is", "that is",
		"in other words", "i.e", "namely", "final figure", "remaining balance", "available",
	}
	// claimCueWeakEN are ordinary predications; they position a value only when
	// no strong cue is present in the sentence.
	claimCueWeakEN = []string{
		"is", "are", "it's", "that's", "there's", "here's", "what's", "have", "has", "own",
		"owns", "there are", "there is", "count", "counted", "get", "about", "roughly",
		"approximately", "around", "exactly", "some", "be", "says", "said", "shows", "showed",
		"lists", "listed", "reads", "records", "recorded", "notes", "noted", "at",
		// Verb-object counts: "took 3 trips", "booked 3 flights", "made 2 payments".
		"took", "take", "takes", "taken", "booked", "book", "books", "made", "make", "makes",
		"had", "did", "done", "completed", "logged", "attended", "visited", "went on",
		"bought", "ordered", "sent", "received", "scheduled", "planned", "hold", "holds",
		"held", "keep", "keeps", "kept", "added", "created", "opened", "flew", "ran",
	}
	claimCueAfterEN = []string{
		"left", "remaining", "remains", "in total", "total", "overall", "net", "altogether",
		"remain", "is left", "are left", "left over",
	}
	// citationEN mark a sentence on an unanswerable case as citing evidence
	// rather than answering ("the only figure on file is ... for March").
	citationEN = []string{"only", "closest", "nearest", "instead", "but", "however", "unrelated", "for a different", "different", "rather", "though", "although", "except"}
	echoEN     = []string{
		"whether", "you asked", "your question", "the question", "you're asking",
		"you are asking", "if it", "asked me", "you wanted to know",
	}
	clarifyEN = []string{
		"which", "what", "could you specify", "can you specify", "could you clarify",
		"can you clarify", "do you mean", "did you mean", "let me know which", "please confirm",
		"please specify", "which one", "to confirm", "i need to know", "tell me which",
		"would you like", "do you want", "shall i", "should i", "can you tell me",
	}
	neitherEN = []string{"neither"}

	declineV13EN = []string{
		"no data", "nothing on file", "not on file", "wasn't recorded", "was never recorded",
		"never recorded", "no entry", "nothing stored", "not stored", "don't have a record",
		"do not have a record", "no mention", "can't confirm", "cannot confirm", "isn't recorded",
		"not recorded", "i have no", "nothing about", "no such", "not available", "unavailable",
		"haven't recorded", "have no record", "have nothing", "not in my records", "not in your records",
		"nothing in my records", "no note", "no notes", "there is no", "there's no", "there are no",
		"there isn't", "there aren't", "not in your", "isn't in your", "nothing for", "no figure",
		"no budget", "no value", "no amount", "was never", "were never", "never gave", "never provided",
	}
	acknowledgeV13EN = []string{
		"removed it", "deleted it", "erased it", "it's removed", "it's deleted", "has been removed",
		"have removed", "have deleted", "i'll forget", "i will forget", "forgetting it", "struck",
		"scrubbed", "taken care of", "handled", "wiped it", "cleared it",
	}
	increaseV13EN = []string{
		"increases", "increasing", "rise", "rises", "risen", "rising", "climbed", "climb", "climbs",
		"grown", "grows", "growing", "growth", "went up", "goes up", "go up", "gone up", "is up",
		"was up", "up by", "moved up", "ticked up", "edged up", "pushed up", "upward", "upwards",
		"gains", "gaining", "net gain", "net positive", "improved", "boosted", "surged", "jumped",
		"expanded", "added to", "more money", "raises", "raising", "higher than before", "rose by",
		"grew by", "increased by", "raised by", "a gain", "in the black", "net increase",
	}
	decreaseV13EN = []string{
		"decreases", "decreasing", "falls", "fallen", "falling", "drop", "drops", "dropping",
		"declined", "shrank", "shrunk", "shrink", "shrinks", "went down", "goes down", "go down",
		"gone down", "is down", "was down", "down by", "moved down", "ticked down", "edged down",
		"downward", "downwards", "loss", "losses", "lost", "losing", "net loss", "net negative",
		"trimmed", "cuts", "cutting", "dipped", "slid", "sank", "contracted", "less money",
		"lowers", "lowering", "reduces", "reducing", "worse off", "fell by", "dropped by",
		"decreased by", "lowered by", "reduced by", "a loss", "in the red", "net decrease",
	}
	unchangedEN = []string{
		"unchanged", "no change", "no net change", "did not change", "didn't change",
		"hasn't changed", "has not changed", "stayed the same", "stays the same",
		"remained the same", "remains the same", "the same as before", "same as before",
		"held steady", "holds steady", "net zero", "zero net", "neither up nor down",
		"neither rose nor fell", "neither increased nor decreased", "broke even", "break even",
		"breaks even", "balanced out", "cancelled out", "canceled out", "cancel out",
		"offset each other", "offset exactly", "no difference", "a wash", "nets out to zero",
		"net effect is zero", "net effect was zero", "no overall change", "no net effect",
		"stayed flat", "remained flat", "stayed put", "did not move", "didn't move",
	}
)

var (
	lexiconCache   = map[string]claimLexicon{}
	lexiconCacheMu sync.Mutex
)

// lexiconFor builds (and caches) the merged claim lexicon for a case language.
// ok is false for a language without a grader lexicon: the grader fails closed.
func lexiconFor(language string) (claimLexicon, bool) {
	key := string(multilingual.Normalize(language))
	lexiconCacheMu.Lock()
	defer lexiconCacheMu.Unlock()
	if lex, ok := lexiconCache[key]; ok {
		return lex, true
	}
	extra, ok := multilingual.For(language)
	if !ok {
		return claimLexicon{}, false
	}
	lex := buildClaimLexicon(extra)
	lexiconCache[key] = lex
	return lex, true
}

func buildClaimLexicon(extra multilingual.Lexicon) claimLexicon {
	fold := func(lists ...[]string) []string {
		seen := map[string]bool{}
		var out []string
		for _, list := range lists {
			for _, p := range list {
				f := foldV13(p)
				if f == "" || seen[f] {
					continue
				}
				seen[f] = true
				out = append(out, f)
			}
		}
		// Longest first so multiword markers win over their prefixes.
		sort.SliceStable(out, func(i, j int) bool { return len(out[i]) > len(out[j]) })
		return out
	}
	foldMap := func(maps ...map[string]int) map[string]int {
		out := map[string]int{}
		for _, m := range maps {
			for k, v := range m {
				out[foldV13(k)] = v
			}
		}
		return out
	}
	numbers := map[string]int{}
	for word, value := range wordToNumber {
		numbers[word] = value
	}
	numbers["once"] = 1
	numbers["twice"] = 2
	lex := claimLexicon{
		rejection:      fold(rejectionEN, extra.Rejection),
		contrast:       fold(contrastEN, extra.Contrast),
		pastStrong:     fold(pastStrongEN, extra.PastStrong),
		pastWeak:       fold(pastWeakEN, extra.PastWeak),
		current:        fold(currentEN, extra.Current),
		correction:     fold(correctionEN, extra.Correction),
		hedge:          fold(hedgeEN, extra.Hedge),
		enumerator:     fold(enumeratorEN, extra.Enumerator),
		claimCueStrong: fold(claimCueStrongEN),
		claimCueWeak:   fold(claimCueWeakEN, extra.ClaimCue),
		claimCueAfter:  fold(claimCueAfterEN),
		citation:       fold(citationEN, extra.Contrast, extra.Hedge),
		echo:           fold(echoEN, extra.Echo),
		clarify:        fold(clarifyEN, extra.Clarify, extra.Interrog),
		decline:        fold(declinePhrases, declineV13EN, extra.Decline),
		acknowledge:    fold(acknowledgementPhrases, acknowledgeV13EN, extra.Acknowledge),
		increase:       fold(increasePhrases, increaseV13EN, extra.Increase),
		decrease:       fold(decreasePhrases, decreaseV13EN, extra.Decrease),
		unchanged:      fold(unchangedEN, extra.Unchanged),
		neither:        fold(neitherEN, []string{"ni", "nem", "né", "weder", "noch"}),
		numberWords:    foldMap(numbers, extra.NumberWords),
		months:         foldMap(monthsEN, extra.Months),
		minorUnit:      fold(minorUnitWordsEN, extra.MinorUnit),
		majorUnit:      fold(majorUnitWordsEN, extra.MajorUnit),
	}
	// Multiword phrases that CONTAIN a rejection word but are not rejections:
	// "no longer", "no change", "don't have", "not sure". They are consumed whole.
	lex.protected = fold(lex.unchanged, lex.decline, lex.pastStrong, lex.current,
		[]string{"not sure", "not certain", "no longer", "not yet", "no problem", "no worries"},
		extra.Decline, extra.Unchanged)
	// Enumerators ("or") are deliberately NOT boundaries: "Lisbon or Oslo" must
	// stay one clause so the enumeration rule sees both values.
	lex.boundary = lex.contrast
	return lex
}

// segment is one clause of the reply with its assertion status.
type segment struct {
	text     string
	sentence int
	rejected bool // opened by a rejection connective or retro-rejected by a correction
	past     bool // strong past marker
	weakPast bool // weak past marker only
	current  bool
	echo     bool // interrogative sentence or echo marker
	hedge    bool
	// afterContrast marks a clause opened by a contrast connective ("but",
	// "however"): it is a fresh assertion, not an appositive of the clause before.
	afterContrast bool
	// afterColon marks a clause the previous clause handed off with a ":"
	// ("$5,000 minus $1,200: $3,800"): its first value is result-cued by that
	// colon even though the boundary split it from the cue.
	afterColon bool
}

// eligible reports whether values in the segment can be asserted candidates.
func (s segment) eligible() bool { return !s.rejected && !s.past && !s.echo }

// analysis is the segmented reply.
type analysis struct {
	slot      string // folded structured answer ("" when absent)
	prose     string // folded final_text
	segments  []segment
	sentences []sentenceSpan
	lex       claimLexicon
}

// analyzeV13 segments a reply for the claim engine.
func analyzeV13(slot, finalText string, lex claimLexicon) analysis {
	an := analysis{slot: foldV13(slot), prose: foldV13(finalText), lex: lex}
	sentences := splitSentencesV13(an.prose)
	an.sentences = sentences
	for si, sent := range sentences {
		if si > 0 && startsWithPhrase(sent.text, lex.correction) {
			for i := range an.segments {
				if an.segments[i].sentence == si-1 {
					an.segments[i].rejected = true
				}
			}
		}
		for _, seg := range segmentSentenceV13(sent.text, lex) {
			seg.sentence = si
			seg.echo = seg.echo || sent.interrogative
			an.segments = append(an.segments, seg)
		}
	}
	return an
}

type sentenceSpan struct {
	text          string
	interrogative bool
}

// splitSentencesV13 splits folded text at sentence terminators. A period
// between two digits is a decimal point, not a terminator.
func splitSentencesV13(text string) []sentenceSpan {
	var out []sentenceSpan
	start := 0
	flush := func(end int, term byte) {
		s := strings.TrimSpace(text[start:end])
		if s != "" {
			s = strings.TrimLeft(s, "¿¡ ")
			out = append(out, sentenceSpan{text: s, interrogative: term == '?' || strings.HasPrefix(strings.TrimSpace(text[start:end]), "¿")})
		}
	}
	for i := 0; i < len(text); i++ {
		c := text[i]
		switch c {
		case '.':
			if i > 0 && i+1 < len(text) && isASCIIDigit(text[i-1]) && isASCIIDigit(text[i+1]) {
				continue
			}
			// "e.g." / "i.e." / "vs." keep the sentence going.
			if i >= 1 && i+1 < len(text) && text[i+1] != ' ' {
				continue
			}
			flush(i, c)
			start = i + 1
		case '!', '?', ';', '\n':
			flush(i, c)
			start = i + 1
		}
	}
	flush(len(text), 0)
	return out
}

func isASCIIDigit(c byte) bool { return c >= '0' && c <= '9' }

// segmentSentenceV13 splits one sentence into clauses by rejection openers,
// contrast/boundary connectives, commas, dashes, and parentheses.
func segmentSentenceV13(sentence string, lex claimLexicon) []segment {
	words := strings.Fields(sentence)
	var out []segment
	var cur []string
	rejected := false
	afterContrast := false
	afterColon := false
	flush := func() {
		if len(cur) > 0 {
			seg := segment{text: strings.Join(cur, " "), rejected: rejected, afterContrast: afterContrast, afterColon: afterColon}
			seg.past = anyBounded(seg.text, lex.pastStrong)
			seg.weakPast = !seg.past && anyBounded(seg.text, lex.pastWeak)
			seg.current = anyBounded(seg.text, lex.current)
			seg.echo = anyBounded(seg.text, lex.echo)
			seg.hedge = anyBounded(seg.text, lex.hedge)
			out = append(out, seg)
		}
		cur = cur[:0]
		afterContrast = false
		afterColon = false
	}
	for i := 0; i < len(words); {
		if n := matchPhraseAt(words, i, lex.protected); n > 0 {
			cur = append(cur, words[i:i+n]...)
			i += n
			continue
		}
		if n := matchPhraseAt(words, i, lex.rejection); n > 0 {
			flush()
			rejected = true
			// Keep the connective in the text so negated direction phrases stay
			// detectable ("not increase"), but the span is already rejected.
			cur = append(cur, words[i:i+n]...)
			i += n
			continue
		}
		if n := matchPhraseAt(words, i, lex.boundary); n > 0 {
			flush()
			rejected = false
			afterContrast = true
			i += n
			continue
		}
		w := words[i]
		cur = append(cur, w)
		i++
		if strings.HasSuffix(w, ",") || strings.HasSuffix(w, ")") || strings.HasSuffix(w, ":") ||
			w == "—" || w == "–" || w == "-" {
			colon := strings.HasSuffix(w, ":")
			flush()
			rejected = false
			afterColon = colon
		}
		if i < len(words) && (strings.HasPrefix(words[i], "(") || words[i] == "—" || words[i] == "–") {
			flush()
			rejected = false
		}
	}
	flush()
	return out
}

// matchPhraseAt returns how many words of phrases match at words[i] (longest
// first; phrases are pre-sorted by length), or 0.
func matchPhraseAt(words []string, i int, phrases []string) int {
	for _, p := range phrases {
		parts := strings.Fields(p)
		if len(parts) == 0 || i+len(parts) > len(words) {
			continue
		}
		ok := true
		for k, part := range parts {
			if strings.Trim(words[i+k], "\"'(),;:!?¿¡«»") != part {
				ok = false
				break
			}
		}
		if ok {
			return len(parts)
		}
	}
	return 0
}

func startsWithPhrase(text string, phrases []string) bool {
	words := strings.Fields(text)
	return matchPhraseAt(words, 0, phrases) > 0
}

func anyBounded(text string, phrases []string) bool {
	for _, p := range phrases {
		if containsBoundedV13(text, p) {
			return true
		}
	}
	return false
}

// extractor returns the typed mentions of one claim kind in a folded text.
type extractor func(text string) []mention

// candidateSet is the engine's output for one claim.
type candidateSet struct {
	keys          []string // distinct asserted candidate keys, first-seen order
	slotUnknown   bool     // slot populated but carries no typed value of this kind
	slotInProse   bool     // some prose clause asserts a value equivalent to the slot
	slotPopulated bool
}

func (c candidateSet) has(key string) bool {
	for _, k := range c.keys {
		if k == key {
			return true
		}
	}
	return false
}

// collect runs the scope rules. equivalent decides whether a prose mention key
// matches a slot mention key (identity by default; money and dates widen it).
// maxClaims is how many values one exposition sentence may assert without a
// cue: 1 for a scalar claim, the item count of that kind for a list claim.
func (an analysis) collect(extract extractor, equivalent func(slotKey, proseKey string) bool, cueRule bool, maxClaims int) candidateSet {
	if equivalent == nil {
		equivalent = func(a, b string) bool { return a == b }
	}
	var out candidateSet
	if an.slot != "" {
		out.slotPopulated = true
		slotMentions := extract(an.slot)
		if len(slotMentions) == 0 {
			out.slotUnknown = true
			out.keys = append(out.keys, "slot:unknown")
			return out
		}
		if strings.TrimSpace(an.prose) == "" {
			for _, m := range slotMentions {
				out.keys = appendKey(out.keys, m.key)
			}
			out.slotInProse = true
			return out
		}
		// A slot value equivalent to (but not identical with) a prose value takes
		// the prose reading: a canonical minor-unit slot beside "$4,110.67" is one
		// candidate, the amount the prose asserted.
		resolved := map[string]string{}
		// Answer clauses: every eligible segment of a sentence in which some
		// eligible segment asserts a value equivalent to the slot. The whole
		// sentence is scoped (not just the matching clause) so "Lisbon and Oslo"
		// cannot hide the second candidate behind a clause boundary, while a
		// distractor in ANOTHER sentence stays protected reasoning (the v12 rule).
		for si := range an.sentences {
			var segs []segment
			var perSeg [][]mention
			matched := false
			for _, seg := range an.segments {
				if seg.sentence != si || !seg.eligible() {
					continue
				}
				ms := extract(seg.text)
				for _, m := range ms {
					for _, sm := range slotMentions {
						if equivalent(sm.key, m.key) {
							matched = true
							if _, done := resolved[sm.key]; !done {
								resolved[sm.key] = m.key
							}
						}
					}
				}
				segs = append(segs, seg)
				perSeg = append(perSeg, ms)
			}
			if !matched {
				continue
			}
			out.slotInProse = true
			for _, m := range sentenceCandidates(segs, perSeg, an.lex, cueRule, maxClaims) {
				if !equivalentToAny(m.key, slotMentions, equivalent) {
					out.keys = appendKey(out.keys, m.key)
				}
			}
		}
		for _, m := range slotMentions {
			key := m.key
			if r, ok := resolved[key]; ok {
				key = r
			}
			out.keys = appendKey(out.keys, key)
		}
		return out
	}
	// No slot: every eligible clause, sentence-level cue priority, weak-past drop.
	type cand struct {
		key      string
		weakPast bool
	}
	var cands []cand
	for si := range an.sentences {
		var segs []segment
		var perSeg [][]mention
		for _, seg := range an.segments {
			if seg.sentence != si || !seg.eligible() {
				continue
			}
			segs = append(segs, seg)
			perSeg = append(perSeg, extract(seg.text))
		}
		for _, m := range sentenceCandidates(segs, perSeg, an.lex, cueRule, maxClaims) {
			cands = append(cands, cand{key: m.key, weakPast: m.weakPast})
		}
	}
	anyCurrent := false
	for _, c := range cands {
		if !c.weakPast {
			anyCurrent = true
		}
	}
	for _, c := range cands {
		if anyCurrent && c.weakPast {
			continue
		}
		out.keys = appendKey(out.keys, c.key)
	}
	return out
}

func equivalentToAny(key string, slot []mention, equivalent func(a, b string) bool) bool {
	for _, sm := range slot {
		if equivalent(sm.key, key) {
			return true
		}
	}
	return false
}

func appendKey(keys []string, key string) []string {
	for _, k := range keys {
		if k == key {
			return keys
		}
	}
	return append(keys, key)
}

// sentenceCandidates applies the sentence-level priority rule across the
// eligible segments of one sentence: cue-positioned values win; otherwise an
// enumeration asserts everything it lists; otherwise a lone value is asserted
// and a multi-value exposition asserts nothing.
func sentenceCandidates(segs []segment, perSeg [][]mention, lex claimLexicon, cueRule bool, maxClaims int) []mention {
	var all, strong, weak []mention
	if !cueRule {
		for i, seg := range segs {
			ms := perSeg[i]
			for k := range ms {
				ms[k].weakPast = seg.weakPast && !seg.current
			}
			all = append(all, ms...)
		}
		return all
	}
	type positioned struct {
		m        mention
		strength int
	}
	var items []positioned
	enumerating, explicit := false, false
	for i, seg := range segs {
		ms := perSeg[i]
		for k := range ms {
			ms[k].weakPast = seg.weakPast && !seg.current
		}
		if seg.hedge || isEnumeration(seg.text, ms, lex) {
			enumerating = true
		}
		first := -1
		for k, m := range ms {
			if first < 0 || m.pos < ms[first].pos {
				first = k
			}
		}
		for k, m := range ms {
			strength := cueStrength(seg.text, m, ms, lex)
			if strength == 0 && seg.afterColon && k == first {
				// The colon that closed the previous clause cues this value.
				strength = 2
			}
			if !m.bare {
				explicit = true
			}
			items = append(items, positioned{m: m, strength: strength})
		}
	}
	for _, it := range items {
		// An uncued bare integer beside an explicitly marked value is
		// exposition ("$3,800 across 4 trips"), unless the clause enumerates.
		if explicit && it.m.bare && it.strength == 0 && !enumerating {
			continue
		}
		all = append(all, it.m)
		switch it.strength {
		case 2:
			strong = append(strong, it.m)
		case 1:
			weak = append(weak, it.m)
		}
	}
	switch {
	case len(strong) > 0:
		return strong
	case enumerating:
		return all
	case len(weak) > 0:
		return weak
	case len(all) <= maxClaims:
		return all
	}
	// Several values, no cue, no enumeration: exposition — unless the past-tense
	// clauses account for all but one ("was X, now Y").
	var current []mention
	for _, m := range all {
		if !m.weakPast {
			current = append(current, m)
		}
	}
	if len(current) >= 1 && len(current) <= maxClaims {
		return current
	}
	return nil
}

// isEnumeration reports whether two mentions in the clause are separated only
// by enumerator tokens ("3800 or 4200", "3800/4200").
func isEnumeration(text string, ms []mention, lex claimLexicon) bool {
	if len(ms) < 2 {
		return false
	}
	sorted := append([]mention(nil), ms...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].pos < sorted[j].pos })
	for i := 1; i < len(sorted); i++ {
		if sorted[i].pos < sorted[i-1].end {
			continue
		}
		raw := text[sorted[i-1].end:sorted[i].pos]
		between := strings.TrimSpace(strings.Trim(strings.TrimSpace(raw), "$€£¥,"))
		if between == "" && strings.Contains(raw, "/") {
			return true
		}
		for _, e := range lex.enumerator {
			if between == e {
				return true
			}
		}
	}
	return false
}

// cueStrength reports how m is positioned: 2 when a strong result cue precedes
// it (within the text since the previous value) or an after-cue follows it, 1
// when only a weak predication precedes it, 0 otherwise.
func cueStrength(text string, m mention, ms []mention, lex claimLexicon) int {
	prevEnd, nextStart := 0, len(text)
	for _, o := range ms {
		if o.end <= m.pos && o.end > prevEnd {
			prevEnd = o.end
		}
		if o.pos >= m.end && o.pos < nextStart {
			nextStart = o.pos
		}
	}
	before := text[prevEnd:m.pos]
	// Only the last few words before the value count as its cue.
	if words := strings.Fields(before); len(words) > 4 {
		before = strings.Join(words[len(words)-4:], " ")
	}
	before = strings.TrimSpace(before)
	before = strings.TrimRight(before, " $€£¥")
	endsWith := func(cue string) bool {
		if before == cue || strings.HasSuffix(before, " "+cue) {
			return true
		}
		// Symbolic cues attach without a space ("= 3800", "→3800", "total:3800").
		return (cue == "=" || cue == "→" || cue == "->" || cue == ":") && strings.HasSuffix(before, cue)
	}
	after := strings.TrimSpace(text[m.end:nextStart])
	if words := strings.Fields(after); len(words) > 3 {
		after = strings.Join(words[:3], " ")
	}
	after = strings.Trim(after, " .,;:!?)")
	// Skip a trailing currency/unit word before testing the after-cue.
	for _, w := range append(append([]string{}, lex.minorUnit...), lex.majorUnit...) {
		after = strings.TrimSpace(strings.TrimPrefix(after, w))
	}
	for _, cue := range lex.claimCueAfter {
		if after == cue || strings.HasPrefix(after, cue+" ") || strings.HasPrefix(after, cue+",") || strings.HasPrefix(after, cue+".") {
			return 2
		}
	}
	for _, cue := range lex.claimCueStrong {
		if endsWith(cue) {
			return 2
		}
	}
	for _, cue := range lex.claimCueWeak {
		if endsWith(cue) {
			return 1
		}
	}
	return 0
}

// knownValueExtractor builds a value-kind extractor over the case's known
// values: expected/accepted forms share the key "expected"; each distractor
// gets its own key. Only known values can be candidates for a value claim.
func knownValueExtractor(expected []string, distractors []string) extractor {
	type known struct {
		form, key string
	}
	var forms []known
	for _, e := range expected {
		if n := normalizeV13(e); n != "" {
			forms = append(forms, known{form: n, key: "expected"})
		}
	}
	for i, d := range distractors {
		if n := normalizeV13(d); n != "" {
			forms = append(forms, known{form: n, key: "d:" + strconv.Itoa(i)})
		}
	}
	return func(text string) []mention {
		var out []mention
		for _, k := range forms {
			if isPureNumber(k.form) {
				// One mention per number-bounded occurrence, so "26" inside
				// "2026" is never positioned as a mention and a repeated number
				// is seen as many times as it occurs.
				for from := 0; ; {
					j := indexNumberTokenV13(text, k.form, from)
					if j < 0 {
						break
					}
					out = append(out, mention{key: k.key, pos: j, end: j + len(k.form)})
					from = j + 1
				}
				continue
			}
			if !strings.Contains(k.form, " ") && commonWords[k.form] {
				continue
			}
			for from := 0; ; {
				j := indexBoundedV13(text, k.form, from)
				if j < 0 {
					break
				}
				out = append(out, mention{key: k.key, pos: j, end: j + len(k.form)})
				from = j + 1
			}
		}
		return out
	}
}

// directionExtractor finds asserted direction kinds in a clause with the
// two-word negation window and the "neither ... nor" rule.
func directionExtractor(lex claimLexicon) extractor {
	return func(text string) []mention {
		var out []mention
		if anyBounded(text, lex.neither) {
			out = append(out, mention{key: "unchanged", pos: 0, end: len(text)})
			return out
		}
		scan := func(phrases []string, key string) {
			for _, p := range phrases {
				for from := 0; ; {
					j := indexBoundedV13(text, p, from)
					if j < 0 {
						break
					}
					if !negatedAt(text, j) {
						out = append(out, mention{key: key, pos: j, end: j + len(p)})
					}
					from = j + 1
				}
			}
		}
		scan(lex.unchanged, "unchanged")
		// Unchanged phrases can contain direction words ("neither rose nor fell",
		// "no net change"); mask them before scanning the polar lexicons.
		masked := text
		for _, p := range lex.unchanged {
			for {
				j := indexBoundedV13(masked, p, 0)
				if j < 0 {
					break
				}
				masked = masked[:j] + strings.Repeat("_", len(p)) + masked[j+len(p):]
			}
		}
		scanMasked := func(phrases []string, key string) {
			for _, p := range phrases {
				for from := 0; ; {
					j := indexBoundedV13(masked, p, from)
					if j < 0 {
						break
					}
					if !negatedAt(masked, j) {
						out = append(out, mention{key: key, pos: j, end: j + len(p)})
					}
					from = j + 1
				}
			}
		}
		scanMasked(lex.increase, "increase")
		scanMasked(lex.decrease, "decrease")
		return out
	}
}
