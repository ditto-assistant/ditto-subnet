// Package textnoise projects deterministic, realistic user-writing errors onto
// generated text. Meaning and ground truth are produced first; this package only
// changes the user-visible surface for benchmark versions that opt into it.
package textnoise

import (
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"
	"unicode"
)

type Kind string

const (
	KindKeyboard       Kind = "keyboard-neighbor"
	KindTranspose      Kind = "transposition"
	KindOmission       Kind = "omission"
	KindDuplication    Kind = "duplication"
	KindCommonSpelling Kind = "common-spelling"
	KindGrammar        Kind = "common-grammar"
)

// Options controls one projection. Protected values are never edited; callers
// use them for answer keys, accepted answers, negative guards, and exact tool
// arguments. MaxEdits defaults from text length when zero.
type Options struct {
	MaxEdits  int
	Grammar   bool
	Protected []string
}

type Stats struct {
	Keyboard       int
	Transpose      int
	Omission       int
	Duplication    int
	CommonSpelling int
	Grammar        int
}

func (s Stats) Total() int {
	return s.Keyboard + s.Transpose + s.Omission + s.Duplication + s.CommonSpelling + s.Grammar
}

func (s *Stats) add(kind Kind) {
	switch kind {
	case KindKeyboard:
		s.Keyboard++
	case KindTranspose:
		s.Transpose++
	case KindOmission:
		s.Omission++
	case KindDuplication:
		s.Duplication++
	case KindCommonSpelling:
		s.CommonSpelling++
	case KindGrammar:
		s.Grammar++
	}
}

// Select returns an exact, stable fraction of ids for a semantic domain. It is
// independent of input order and guarantees at least one selected item whenever
// bps and ids are non-zero. This avoids seed-specific coverage holes.
func Select(seed int64, domain string, ids []string, bps int) map[string]bool {
	selected := map[string]bool{}
	if bps <= 0 || len(ids) == 0 {
		return selected
	}
	if bps > 10_000 {
		bps = 10_000
	}
	unique := make(map[string]bool, len(ids))
	type ranked struct {
		id   string
		rank uint64
	}
	items := make([]ranked, 0, len(ids))
	for _, id := range ids {
		if id == "" || unique[id] {
			continue
		}
		unique[id] = true
		items = append(items, ranked{id: id, rank: hash64(seed, domain, id)})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].rank != items[j].rank {
			return items[i].rank < items[j].rank
		}
		return items[i].id < items[j].id
	})
	quota := (len(items)*bps + 9_999) / 10_000
	if quota > len(items) {
		quota = len(items)
	}
	for _, item := range items[:quota] {
		selected[item.id] = true
	}
	return selected
}

// Project adds bounded writing noise without altering protected ground truth.
// Its entropy comes from the chosen token, mechanism, position, and keyboard
// neighbor, all derived from (seed, identity) rather than a closed typo table.
func Project(text string, seed int64, identity string, opts Options) (string, Stats) {
	if text == "" {
		return text, Stats{}
	}
	protected := protectedWords(opts.Protected)
	tokens := eligibleTokens(text, protected)
	if len(tokens) == 0 {
		return text, Stats{}
	}
	edits := opts.MaxEdits
	if edits <= 0 {
		switch {
		case len(tokens) >= 80:
			edits = 4
		case len(tokens) >= 35:
			edits = 3
		case len(tokens) >= 14:
			edits = 2
		default:
			edits = 1
		}
	}
	if edits > len(tokens) {
		edits = len(tokens)
	}
	r := rand.New(rand.NewSource(int64(hash64(seed, "project", identity) & ((1 << 63) - 1))))
	var stats Stats

	// Common grammatical errors are phrase-level, so apply at most one before
	// token edits and then re-tokenize. Other edits remain independent.
	if opts.Grammar && edits > 0 && r.Intn(100) < 32 {
		if projected, ok := projectGrammar(text, r, protected); ok {
			text = projected
			stats.add(KindGrammar)
			edits--
		}
	}
	if edits == 0 {
		return text, stats
	}
	tokens = eligibleTokens(text, protected)
	if edits > len(tokens) {
		edits = len(tokens)
	}
	perm := r.Perm(len(tokens))[:edits]
	type replacement struct {
		start, end int
		value      string
		kind       Kind
	}
	replacements := make([]replacement, 0, edits)
	for _, index := range perm {
		token := tokens[index]
		value, kind := mutateWord(token.word, r)
		if value == token.word {
			continue
		}
		replacements = append(replacements, replacement{start: token.start, end: token.end, value: value, kind: kind})
	}
	sort.Slice(replacements, func(i, j int) bool { return replacements[i].start > replacements[j].start })
	for _, replacement := range replacements {
		text = text[:replacement.start] + replacement.value + text[replacement.end:]
		stats.add(replacement.kind)
	}
	return text, stats
}

type token struct {
	start int
	end   int
	word  string
}

func eligibleTokens(text string, protected map[string]bool) []token {
	all := scanWords(text)
	out := make([]token, 0, len(all))
	for _, candidate := range all {
		lower := strings.ToLower(candidate.word)
		letters := strings.ReplaceAll(lower, "'", "")
		if len(letters) < 4 || len(letters) > 22 || protected[lower] || protected[letters] {
			continue
		}
		if allUpper(candidate.word) || unsafeSegment(text, candidate.start, candidate.end) {
			continue
		}
		out = append(out, candidate)
	}
	return out
}

func scanWords(text string) []token {
	var out []token
	start := -1
	for i := 0; i <= len(text); i++ {
		letter := i < len(text) && ((text[i] >= 'a' && text[i] <= 'z') || (text[i] >= 'A' && text[i] <= 'Z') || text[i] == '\'')
		if letter && start < 0 {
			start = i
		}
		if !letter && start >= 0 {
			word := text[start:i]
			if strings.Trim(word, "'") != "" {
				out = append(out, token{start: start, end: i, word: word})
			}
			start = -1
		}
	}
	return out
}

func unsafeSegment(text string, start, end int) bool {
	left := start
	for left > 0 && !unicode.IsSpace(rune(text[left-1])) {
		left--
	}
	right := end
	for right < len(text) && !unicode.IsSpace(rune(text[right])) {
		right++
	}
	segment := text[left:right]
	if strings.ContainsAny(segment, "@/$=\\") || strings.Contains(segment, "://") {
		return true
	}
	for _, r := range segment {
		if unicode.IsDigit(r) {
			return true
		}
	}
	return false
}

func protectedWords(values []string) map[string]bool {
	out := map[string]bool{"ditto": true, "dittocode": true, "code": true}
	for _, value := range values {
		for _, token := range scanWords(value) {
			lower := strings.ToLower(token.word)
			if len(strings.ReplaceAll(lower, "'", "")) >= 3 {
				out[lower] = true
				out[strings.ReplaceAll(lower, "'", "")] = true
			}
		}
	}
	return out
}

func allUpper(word string) bool {
	hasLetter := false
	for _, r := range word {
		if unicode.IsLetter(r) {
			hasLetter = true
			if unicode.IsLower(r) {
				return false
			}
		}
	}
	return hasLetter
}

func mutateWord(word string, r *rand.Rand) (string, Kind) {
	draw := r.Intn(100)
	switch {
	case draw < 27:
		return keyboardNeighbor(word, r), KindKeyboard
	case draw < 48:
		return transpose(word, r), KindTranspose
	case draw < 67:
		return omit(word, r), KindOmission
	case draw < 79:
		return duplicate(word, r), KindDuplication
	default:
		if misspelled, ok := commonMisspellings[strings.ToLower(word)]; ok {
			return matchCase(word, misspelled), KindCommonSpelling
		}
		return keyboardNeighbor(word, r), KindKeyboard
	}
}

func keyboardNeighbor(word string, r *rand.Rand) string {
	chars := []byte(word)
	positions := letterPositions(chars, false)
	if len(positions) == 0 {
		return word
	}
	position := positions[r.Intn(len(positions))]
	lower := chars[position]
	upper := lower >= 'A' && lower <= 'Z'
	if upper {
		lower += 'a' - 'A'
	}
	neighbors := keyboardNeighbors[lower]
	if neighbors == "" {
		return transpose(word, r)
	}
	replacement := neighbors[r.Intn(len(neighbors))]
	if upper {
		replacement -= 'a' - 'A'
	}
	chars[position] = replacement
	return string(chars)
}

func transpose(word string, r *rand.Rand) string {
	chars := []byte(word)
	var positions []int
	for i := 1; i+2 < len(chars); i++ {
		if isASCIIAlpha(chars[i]) && isASCIIAlpha(chars[i+1]) && strings.ToLower(string(chars[i])) != strings.ToLower(string(chars[i+1])) {
			positions = append(positions, i)
		}
	}
	if len(positions) == 0 {
		return keyboardNeighbor(word, r)
	}
	i := positions[r.Intn(len(positions))]
	chars[i], chars[i+1] = chars[i+1], chars[i]
	return string(chars)
}

func omit(word string, r *rand.Rand) string {
	chars := []byte(word)
	positions := letterPositions(chars, false)
	if len(positions) == 0 {
		return transpose(word, r)
	}
	i := positions[r.Intn(len(positions))]
	return string(append(chars[:i], chars[i+1:]...))
}

func duplicate(word string, r *rand.Rand) string {
	chars := []byte(word)
	positions := letterPositions(chars, false)
	if len(positions) == 0 {
		return word
	}
	i := positions[r.Intn(len(positions))]
	out := make([]byte, 0, len(chars)+1)
	out = append(out, chars[:i+1]...)
	out = append(out, chars[i])
	out = append(out, chars[i+1:]...)
	return string(out)
}

func letterPositions(chars []byte, allowEdges bool) []int {
	var out []int
	for i, char := range chars {
		if !isASCIIAlpha(char) || (!allowEdges && (i == 0 || i == len(chars)-1)) {
			continue
		}
		out = append(out, i)
	}
	return out
}

func isASCIIAlpha(char byte) bool {
	return (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z')
}

func matchCase(source, replacement string) string {
	if source != "" && source[0] >= 'A' && source[0] <= 'Z' {
		return strings.ToUpper(replacement[:1]) + replacement[1:]
	}
	return replacement
}

type grammarRule struct{ from, to string }

var grammarRules = []grammarRule{
	{"could have", "could of"}, {"should have", "should of"}, {"would have", "would of"},
	{"supposed to", "suppose to"}, {"used to", "use to"}, {"a lot", "alot"},
	{"it's", "its"}, {"who's", "whose"}, {"doesn't", "doesnt"},
	{"don't", "dont"}, {"i'm", "im"},
}

func projectGrammar(text string, r *rand.Rand, protected map[string]bool) (string, bool) {
	lower := strings.ToLower(text)
	type candidate struct {
		start int
		rule  grammarRule
	}
	var candidates []candidate
	for _, rule := range grammarRules {
		if ruleProtected(rule.from, protected) {
			continue
		}
		for offset := 0; offset < len(lower); {
			index := strings.Index(lower[offset:], rule.from)
			if index < 0 {
				break
			}
			index += offset
			end := index + len(rule.from)
			if wordBoundary(lower, index, end) {
				candidates = append(candidates, candidate{start: index, rule: rule})
			}
			offset = end
		}
	}
	if len(candidates) == 0 {
		return text, false
	}
	choice := candidates[r.Intn(len(candidates))]
	return text[:choice.start] + matchCase(text[choice.start:choice.start+len(choice.rule.from)], choice.rule.to) + text[choice.start+len(choice.rule.from):], true
}

func ruleProtected(value string, protected map[string]bool) bool {
	for _, token := range scanWords(value) {
		if protected[strings.ToLower(token.word)] {
			return true
		}
	}
	return false
}

func wordBoundary(text string, start, end int) bool {
	left := start == 0 || !isASCIIAlpha(text[start-1])
	right := end == len(text) || !isASCIIAlpha(text[end])
	return left && right
}

func hash64(seed int64, parts ...string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte("dittobench-v8-text-noise"))
	_, _ = h.Write([]byte{0})
	var seedBytes [8]byte
	for i := range seedBytes {
		seedBytes[i] = byte(uint64(seed) >> (8 * i))
	}
	_, _ = h.Write(seedBytes[:])
	for _, part := range parts {
		_, _ = h.Write([]byte{0})
		_, _ = h.Write([]byte(part))
	}
	return h.Sum64()
}

var keyboardNeighbors = map[byte]string{
	'a': "qwsz", 'b': "vghn", 'c': "xdfv", 'd': "serfcx", 'e': "wsdr", 'f': "drtgvc",
	'g': "ftyhbv", 'h': "gyujnb", 'i': "ujko", 'j': "huikmn", 'k': "jiolm", 'l': "kop",
	'm': "njk", 'n': "bhjm", 'o': "iklp", 'p': "ol", 'q': "wa", 'r': "edft",
	's': "awedxz", 't': "rfgy", 'u': "yhji", 'v': "cfgb", 'w': "qase", 'x': "zsdc",
	'y': "tghu", 'z': "asx",
}

var commonMisspellings = map[string]string{
	"accommodate": "accomodate", "address": "adress", "apparently": "apparantly",
	"available": "availible", "beginning": "begining", "business": "buisness",
	"calendar": "calender", "colleague": "collegue", "commitment": "committment",
	"correction": "corection", "corrected": "corected", "decision": "decison",
	"definitely": "definately", "description": "descripton", "environment": "enviroment",
	"experience": "experiance", "financial": "finacial", "friend": "freind",
	"immediately": "immediatly", "independent": "independant", "itinerary": "itenerary",
	"maintenance": "maintainance", "necessary": "neccessary", "occurred": "occured",
	"opportunity": "oppertunity", "original": "orginal", "personal": "personel",
	"preferred": "prefered", "privilege": "priviledge", "professional": "proffesional",
	"project": "proejct", "receive": "recieve", "recommendation": "reccomendation",
	"reference": "referance", "relationship": "relashionship", "relevant": "relevent",
	"remember": "remeber", "restaurant": "restaraunt", "schedule": "schedual",
	"separate": "seperate", "successful": "succesful", "through": "thruogh",
	"together": "toghether", "tomorrow": "tommorow", "whether": "wether",
}
