package textnoise

import (
	"math/rand"
	"sort"
	"strings"
)

// Typo projector v2 (bench_version >= 13).
//
// The v1 projector (Project) is a QWERTY/ASCII single-edit engine with a closed
// 47-word misspelling table; the v11 pass narrowed it further to a ~65-word
// framing safelist, and the v12 board was won by anchoring on exactly the tokens
// those passes could never touch. v2 inverts the design:
//
//   - the keyboard layout is drawn per (seed, salt) from QWERTY, AZERTY, QWERTZ
//     and a fat-finger mobile layout, so neighbour tables differ per dataset;
//   - a chosen token receives 1-3 edits, bounded by floor(len/3) (minimum 1),
//     so long framing words drift far enough that single-edit fuzzy recovery
//     fails while short words stay legible;
//   - edits come from keyboard neighbours, transpositions, omissions,
//     duplications, phonetic digraph swaps and autocorrect-style vowel
//     confusions rather than from a closed misspelling table;
//   - ANY token class is eligible except protected values, so there is no
//     safelist for an adversary to invert; instead a meaning-preserving
//     neighbour check rejects every edit whose result is a different real word
//     ("not" -> "now", "form" -> "from") and the polarity/temporal/comparative
//     vocabulary a question's meaning rides on is never edited at all;
//   - word edges are preserved so the result reads through for a human or a
//     model.
//
// Determinism: every draw comes from a hash of (seed, salt, identity). v1
// callers are untouched; Project keeps its bytes for every earlier version.

// Layout names one keyboard neighbour table.
type Layout string

const (
	LayoutQWERTY Layout = "qwerty"
	LayoutAZERTY Layout = "azerty"
	LayoutQWERTZ Layout = "qwertz"
	LayoutMobile Layout = "mobile"
)

// Layouts is the per-seed layout pool, in draw order.
var Layouts = []Layout{LayoutQWERTY, LayoutAZERTY, LayoutQWERTZ, LayoutMobile}

const (
	KindPhonetic    Kind = "phonetic-substitution"
	KindAutocorrect Kind = "autocorrect-vowel"
)

// OptionsV2 controls one v2 projection. Layout defaults to LayoutForSeed.
// MaxTokens is the number of tokens to edit; zero derives it from the token
// count (one per ten eligible tokens, at least one, at most six). Protected
// values are never edited, and neither is any word in the meaning-critical set.
type OptionsV2 struct {
	Layout    Layout
	MaxTokens int
	Protected []string
	// Force disables the proper-noun proxy (sentence-interior or adjacent
	// capitalised tokens are skipped by default because, in generated prose,
	// they mark names, aliases and labels the pass cannot otherwise identify).
	// A caller that projects a value it KNOWS must be misspelled — the v13
	// alias misspelling of a capitalised setting such as a font name — sets it
	// so the value is never rendered verbatim. Protected values and
	// meaning-critical vocabulary stay untouchable either way.
	Force bool
}

// MaxEditsForToken is the v2 per-token edit bound: floor(len/3) clamped to [1, 3].
func MaxEditsForToken(word string) int {
	letters := 0
	for i := 0; i < len(word); i++ {
		if isASCIIAlpha(word[i]) {
			letters++
		}
	}
	bound := letters / 3
	if bound < 1 {
		bound = 1
	}
	if bound > 3 {
		bound = 3
	}
	return bound
}

// LayoutForSeed draws the dataset's keyboard layout from (seed, salt).
func LayoutForSeed(seed int64, salt uint64) Layout {
	return Layouts[int(hashV2(seed, salt, "layout")%uint64(len(Layouts)))]
}

// ProjectV2 adds bounded v2 writing noise to text without altering protected
// ground truth or meaning-critical vocabulary.
func ProjectV2(text string, seed int64, salt uint64, identity string, opts OptionsV2) (string, Stats) {
	if text == "" {
		return text, Stats{}
	}
	layout := opts.Layout
	if layout == "" {
		layout = LayoutForSeed(seed, salt)
	}
	neighbors := layoutNeighbors[layout]
	if neighbors == nil {
		neighbors = keyboardNeighbors
	}
	protected := protectedWords(opts.Protected)
	tokens := eligibleTokensV2(text, protected, opts.Force)
	if len(tokens) == 0 {
		return text, Stats{}
	}
	budget := opts.MaxTokens
	if budget <= 0 {
		budget = (len(tokens) + 9) / 10
		if budget > 6 {
			budget = 6
		}
	}
	if budget > len(tokens) {
		budget = len(tokens)
	}
	r := rand.New(rand.NewSource(int64(hashV2(seed, salt, "project:"+identity) & ((1 << 63) - 1))))
	perm := r.Perm(len(tokens))[:budget]
	type replacement struct {
		start, end int
		value      string
		kinds      []Kind
	}
	replacements := make([]replacement, 0, budget)
	for _, index := range perm {
		token := tokens[index]
		value, kinds := mutateWordV2(token.word, r, neighbors, protected)
		if value == token.word {
			continue
		}
		replacements = append(replacements, replacement{start: token.start, end: token.end, value: value, kinds: kinds})
	}
	sort.Slice(replacements, func(i, j int) bool { return replacements[i].start > replacements[j].start })
	var stats Stats
	for _, replacement := range replacements {
		text = text[:replacement.start] + replacement.value + text[replacement.end:]
		for _, kind := range replacement.kinds {
			stats.add(kind)
		}
	}
	return text, stats
}

// eligibleTokensV2 keeps every alphabetic token of four or more letters that is
// not a protected value, a meaning-critical word, an all-caps token, part of a
// machine-like segment (digits, @, /, $, =, _, URLs), a quoted span, or — unless
// force is set — a capitalised token in sentence-interior position (the
// proper-noun proxy for names, aliases and labels the artifact pass cannot
// otherwise identify).
func eligibleTokensV2(text string, protected map[string]bool, force bool) []token {
	all := scanWords(text)
	out := make([]token, 0, len(all))
	for index, candidate := range all {
		lower := strings.ToLower(candidate.word)
		letters := strings.ReplaceAll(lower, "'", "")
		if len(letters) < 4 || len(letters) > 22 || protected[lower] || protected[letters] || meaningCritical[letters] {
			continue
		}
		if strings.Contains(candidate.word, "'") {
			// Contractions carry polarity (don't, isn't, wasn't); leave them.
			continue
		}
		if allUpper(candidate.word) || unsafeSegmentV2(text, candidate.start, candidate.end) || insideQuotes(text, candidate.start) {
			continue
		}
		if !force && (isInteriorCapitalised(text, candidate) || isCapitalisedPhrase(all, index)) {
			continue
		}
		out = append(out, candidate)
	}
	return out
}

func unsafeSegmentV2(text string, start, end int) bool {
	if unsafeSegment(text, start, end) {
		return true
	}
	left := start
	for left > 0 && text[left-1] != ' ' && text[left-1] != '\n' && text[left-1] != '\t' {
		left--
	}
	right := end
	for right < len(text) && text[right] != ' ' && text[right] != '\n' && text[right] != '\t' {
		right++
	}
	return strings.ContainsAny(text[left:right], "_%#<>[]{}|")
}

// insideQuotes reports whether position pos sits inside an open “…” or "…"
// span. Quoted spans carry names, aliases and stored values verbatim.
func insideQuotes(text string, pos int) bool {
	prefix := text[:pos]
	curly := strings.Count(prefix, "“") - strings.Count(prefix, "”")
	if curly > 0 {
		return true
	}
	return strings.Count(prefix, "\"")%2 == 1
}

// isInteriorCapitalised reports whether a capitalised token is NOT at the start
// of a sentence or line, which in generated prose marks a name, alias or label.
func isInteriorCapitalised(text string, tok token) bool {
	if tok.word[0] < 'A' || tok.word[0] > 'Z' {
		return false
	}
	if tok.word == "I" {
		return false
	}
	i := tok.start - 1
	for i >= 0 && (text[i] == ' ' || text[i] == '\t') {
		i--
	}
	if i < 0 {
		return false
	}
	switch text[i] {
	case '.', '!', '?', ':', '\n', '"', '(', '\'', '-', ';':
		return false
	}
	// A multi-byte punctuation mark (an em dash or curly quote) ends in a
	// non-ASCII byte and opens a clause; any ASCII letter, digit or comma before
	// the token marks sentence interior.
	return text[i] < 0x80
}

// isCapitalisedPhrase reports whether a capitalised token is adjacent to
// another capitalised token ("Taylor Martinez", "Norrford Group"): a
// multi-word proper noun, even at sentence start, is a name and is left alone.
func isCapitalisedPhrase(all []token, index int) bool {
	capitalised := func(word string) bool {
		return word != "I" && word[0] >= 'A' && word[0] <= 'Z' && !allUpper(word)
	}
	if !capitalised(all[index].word) {
		return false
	}
	if index+1 < len(all) && all[index+1].start == all[index].end+1 && capitalised(all[index+1].word) {
		return true
	}
	return index > 0 && all[index-1].end+1 == all[index].start && capitalised(all[index-1].word)
}

// mutateWordV2 applies 1..MaxEditsForToken edits and keeps the result only when
// it is not a different real word, not a protected token, and still differs from
// the original. It retries a handful of times before leaving the word alone.
func mutateWordV2(word string, r *rand.Rand, neighbors map[byte]string, protected map[string]bool) (string, []Kind) {
	bound := MaxEditsForToken(word)
	for attempt := 0; attempt < 6; attempt++ {
		edits := 1 + r.Intn(bound)
		value := word
		kinds := make([]Kind, 0, edits)
		for e := 0; e < edits; e++ {
			next, kind := mutateOnceV2(value, r, neighbors)
			if next == value {
				continue
			}
			value = next
			kinds = append(kinds, kind)
		}
		lower := strings.ToLower(value)
		if value == word || len(kinds) == 0 {
			continue
		}
		if realWords[lower] || protected[lower] || meaningCritical[lower] {
			continue
		}
		if value[0] != word[0] || value[len(value)-1] != word[len(word)-1] {
			continue
		}
		// Composed edits can overlap (a transposition followed by a swap inside
		// it) and read as more edits than were drawn; the published bound is on
		// the resulting distance, so measure it.
		if EditDistance(word, value) > bound {
			continue
		}
		return value, kinds
	}
	return word, nil
}

func mutateOnceV2(word string, r *rand.Rand, neighbors map[byte]string) (string, Kind) {
	draw := r.Intn(100)
	switch {
	case draw < 30:
		return keyboardNeighborIn(word, r, neighbors), KindKeyboard
	case draw < 50:
		return transpose(word, r), KindTranspose
	case draw < 68:
		return omit(word, r), KindOmission
	case draw < 80:
		return duplicate(word, r), KindDuplication
	case draw < 92:
		if out, ok := phoneticSubstitute(word, r); ok {
			return out, KindPhonetic
		}
		return keyboardNeighborIn(word, r, neighbors), KindKeyboard
	default:
		if out, ok := vowelSwap(word, r); ok {
			return out, KindAutocorrect
		}
		return transpose(word, r), KindTranspose
	}
}

func keyboardNeighborIn(word string, r *rand.Rand, neighbors map[byte]string) string {
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
	options := neighbors[lower]
	if options == "" {
		return transpose(word, r)
	}
	replacement := options[r.Intn(len(options))]
	if upper {
		replacement -= 'a' - 'A'
	}
	chars[position] = replacement
	return string(chars)
}

type phoneticRule struct{ from, to string }

// phoneticRules are interior digraph confusions a phone or a hurried writer
// produces; each applies to any word that contains the pattern away from its
// edges, so no closed word table is involved.
var phoneticRules = []phoneticRule{
	{"ph", "f"}, {"ck", "k"}, {"ie", "ei"}, {"ei", "ie"}, {"ea", "ee"}, {"ee", "ea"},
	{"ou", "ow"}, {"tion", "shun"}, {"sion", "shun"}, {"er", "ur"}, {"ll", "l"}, {"ss", "s"},
	{"ai", "ay"}, {"igh", "i"}, {"ough", "uff"}, {"que", "k"}, {"ce", "se"}, {"ci", "si"},
	{"mm", "m"}, {"tt", "t"}, {"rr", "r"}, {"nn", "n"}, {"pp", "p"}, {"oo", "u"},
}

func phoneticSubstitute(word string, r *rand.Rand) (string, bool) {
	lower := strings.ToLower(word)
	type hit struct {
		at   int
		rule phoneticRule
	}
	var hits []hit
	for _, rule := range phoneticRules {
		for offset := 1; offset < len(lower); {
			index := strings.Index(lower[offset:], rule.from)
			if index < 0 {
				break
			}
			index += offset
			if index >= 1 && index+len(rule.from) < len(lower) {
				hits = append(hits, hit{at: index, rule: rule})
			}
			offset = index + 1
		}
	}
	if len(hits) == 0 {
		return word, false
	}
	choice := hits[r.Intn(len(hits))]
	return word[:choice.at] + choice.rule.to + word[choice.at+len(choice.rule.from):], true
}

// vowelSwap replaces one interior vowel with another, the autocorrect-shaped
// confusion behind "seperate" and "definately".
func vowelSwap(word string, r *rand.Rand) (string, bool) {
	const vowels = "aeiou"
	chars := []byte(word)
	var positions []int
	for i := 1; i+1 < len(chars); i++ {
		if strings.IndexByte(vowels, chars[i]) >= 0 {
			positions = append(positions, i)
		}
	}
	if len(positions) == 0 {
		return word, false
	}
	i := positions[r.Intn(len(positions))]
	replacement := vowels[r.Intn(len(vowels))]
	if replacement == chars[i] {
		replacement = vowels[(strings.IndexByte(vowels, chars[i])+1)%len(vowels)]
	}
	chars[i] = replacement
	return string(chars), true
}

func hashV2(seed int64, salt uint64, identity string) uint64 {
	var saltText [16]byte
	const digits = "0123456789abcdef"
	for i := range saltText {
		saltText[i] = digits[(salt>>(4*uint(15-i)))&0xf]
	}
	return hash64(seed, "v2", string(saltText[:]), identity)
}

// layoutNeighbors holds one neighbour table per layout. Tables are built from
// row strings at init in a fixed order, so lookups are deterministic.
var layoutNeighbors = map[Layout]map[byte]string{
	LayoutQWERTY: keyboardNeighbors,
	LayoutAZERTY: buildNeighbors([]string{"azertyuiop", "qsdfghjklm", "wxcvbn"}, 1),
	LayoutQWERTZ: buildNeighbors([]string{"qwertzuiop", "asdfghjkl", "yxcvbnm"}, 1),
	// A phone keyboard: the same key rows as QWERTY, but a thumb lands two keys
	// away almost as often as one, so the neighbourhood is wider.
	LayoutMobile: buildNeighbors([]string{"qwertyuiop", "asdfghjkl", "zxcvbnm"}, 2),
}

// buildNeighbors derives a neighbour table from keyboard rows: keys within
// reach positions on the same row plus the keys directly above and below.
func buildNeighbors(rows []string, reach int) map[byte]string {
	out := make(map[byte]string, 26)
	for rowIndex, row := range rows {
		for col := 0; col < len(row); col++ {
			var b strings.Builder
			for d := -reach; d <= reach; d++ {
				if d == 0 || col+d < 0 || col+d >= len(row) {
					continue
				}
				b.WriteByte(row[col+d])
			}
			for _, other := range []int{rowIndex - 1, rowIndex + 1} {
				if other < 0 || other >= len(rows) {
					continue
				}
				for d := -1; d <= 0; d++ {
					if col+d >= 0 && col+d < len(rows[other]) {
						b.WriteByte(rows[other][col+d])
					}
				}
			}
			out[row[col]] = b.String()
		}
	}
	return out
}

// meaningCritical words are never edited: a typo on any of them can flip the
// sense of a question (polarity, temporal order, comparison, direction), so a
// meaning-preserving projector must leave them byte-exact. This is a
// protect-set, not an edit safelist: everything outside it and outside the
// protected values remains eligible.
var meaningCritical = map[string]bool{
	"not": true, "never": true, "none": true, "nothing": true, "nobody": true, "without": true,
	"except": true, "only": true, "dont": true, "doesnt": true, "didnt": true, "isnt": true,
	"wasnt": true, "arent": true, "werent": true, "cannot": true, "cant": true, "wont": true,
	"before": true, "after": true, "earlier": true, "later": true, "previous": true, "prior": true,
	"current": true, "currently": true, "latest": true, "original": true, "originally": true,
	"first": true, "last": true, "older": true, "newer": true, "former": true, "initial": true,
	"final": true, "earliest": true, "afterward": true, "afterwards": true, "since": true, "until": true,
	"increase": true, "increased": true, "decrease": true, "decreased": true, "raise": true, "raised": true,
	"lower": true, "lowered": true, "higher": true, "highest": true, "lowest": true, "more": true, "less": true,
	"most": true, "least": true, "longer": true, "longest": true, "shorter": true, "shortest": true,
	"larger": true, "largest": true, "smaller": true, "smallest": true, "above": true, "below": true,
	"add": true, "added": true, "adding": true, "subtract": true, "subtracted": true, "minus": true,
	"plus": true, "gain": true, "gaining": true, "loss": true, "losing": true, "remove": true, "removed": true,
	"delete": true, "deleted": true, "keep": true, "instead": true, "rather": true, "than": true, "then": true,
	"still": true, "already": true, "again": true, "once": true, "twice": true, "each": true, "every": true,
	"all": true, "both": true, "either": true, "neither": true, "other": true, "another": true, "same": true,
	"different": true, "changed": true, "unchanged": true, "replace": true, "replaced": true, "replacing": true,
	"corrected": true, "correction": true, "approved": true, "paid": true, "unpaid": true, "owed": true,
	"outstanding": true, "remaining": true, "remains": true, "left": true, "total": true, "net": true,
	"cents": true, "dollars": true, "days": true, "hours": true, "minutes": true, "weeks": true,
	"actually": true, "actual": true, "real": true, "really": true, "exact": true, "exactly": true,
	"between": true, "within": true, "against": true, "across": true, "through": true, "throughout": true,
	"whether": true, "unless": true, "while": true, "during": true, "because": true, "though": true,
	"although": true, "however": true, "otherwise": true, "yes": true, "true": true, "false": true,
	"yesterday": true, "today": true, "tomorrow": true, "friday": true, "monday": true, "tuesday": true,
	"wednesday": true, "thursday": true, "saturday": true, "sunday": true,
	"yours": true, "mine": true, "theirs": true, "ours": true, "own": true, "someone": true, "anyone": true,
	"ignore": true, "skip": true, "stop": true, "cancel": true, "start": true, "begin": true,
	"personal": true, "business": true, "internal": true, "external": true, "private": true, "public": true,
}

// realWords is the neighbour lexicon behind the meaning-preserving check: an
// edit whose result lands on any of these (and differs from the source) is
// rejected, so a typo can never turn one common word into another.
var realWords = func() map[string]bool {
	out := make(map[string]bool, 512)
	for _, word := range strings.Fields(realWordList) {
		out[word] = true
	}
	for word := range meaningCritical {
		out[word] = true
	}
	return out
}()

const realWordList = `
the and for are but not you all any can had her was one our out day get has him his how man new now old see two
way who boy did its let put say she too use dad mom now nor note nod notes form from frame farm firm fork forms
than then that them they thee this thus these those there their theirs here hear heard herd where were wear
we're ware wire wore worn word work world words would could should shall will with wish which witch while whole
hole home hope hose host hour house how our out about above after again against along also always among
another answer anyone around because become been before began begin behind being below best better between
big body book both bring brought build built call came cannot car care carry case cause change check child
children city class clean clear close cold color come common could country course cover cross cut dark data
deal dear deep does done door down draw drive during each early earth east easy eat edit else end enough
even ever every fact fall false family fast father feel feet fell felt few field file fill final find fine
fire first five floor fold follow food foot force found four free friend front full game gave give given
glass goes going gold gone good got great green ground group grow guess half hall hand hang happy hard have
head heat held help hence high hill hold hot huge idea inch into iron item join just keep kept kind king know
known land large last late lead learn least leave left less life light like line list little live load long
look lose lost lot loud love made mail main make many mark mass may mean meat meet might mile mind mine miss
mode moon more most mother move much music must name near need never news next nice night nine none noon
north nose nothing number ocean often once only open order other over page paid pain pair paper part pass
past path pay peace people place plain plan plane plant play point poor port post pound power press
price print pull push quick quiet quit quite race rain raise ran rate read ready real record red rest ride
right ring rise river road rock roll room root rose round rule run safe said sail salt same sand save saw
scale school sea seat second seed seem seen sell send sense sent serve set seven shape share sharp ship shop
short shot show side sign since sing sister sit six size skin sky sleep slip slow small smile snow soft soil
sold some song soon sort sound south space speak speed spell spend spent spot spread spring square stand star
stat state stay step stick still stood stop store storm story straight strange street strong study such sudden
suit sum summer sun sure table tail take talk tall team tell ten term test than thank thick thin thing think
third though thought three through throw tie time tiny tire told tone took tool tools top total touch toward
town track trade train tree trip true try turn type under unit until upon very view visit voice vote wait walk
wall want warm wash watch water wave week weight well went west wheel when whether white why wide wife wild
win wind window winter wise woman women wonder wood word wore worse worst worth write wrong wrote yard year yes
yet young your yours zero able acid aged also area army away baby back ball bank base bath bear beat bed
beer bell belt bird bite black blade blame blind block blood blow blue board boat bond bone born boss bowl
box brain brand bread break breath brief broad brown brush bulk burn bus busy buy cake calm camp cap card
cash cast cat catch cell cent chain chair chart chat cheap chest chief chip claim clip cloud club coal coast
coat code coin cool cope copy core corn cost count court cow crash cream crew crime crop crowd cup cure curve
dance date dawn dead deaf debt deck deny desk dish dog doll doubt dozen draft dress drink drop drug drum dry
duck dust duty ease edge egg eight elder empty entry equal era error essay even event evil exam exit face fade
fail fair faith fame fan far farm fat fate fault fear fee feed fence fever fill film fish fit fix flag flat
flesh float flood flow fly fog folk fool forth fox frank fresh fruit fuel fun fund fur gain gap gas gate gear
gene gift girl glad glow goal god grain grand grant grass grave gray grey grip guard guide gun gut guy habit
hair hat hate heal heart heavy hell hero hide hint hire hit hook horse hunt hurt ice ill image index inner
input jail jazz jet job joke joy judge juice jump jury key kick kid kill knee knife knock lack lady lake lamp
lane lap law lawn lay layer lazy leaf lean leg lend level lid lie lift limb limit link lip lock log loop
lord loss luck lunch lung mad magic major male map march mask match mate math meal meant medal melt menu mere
mess metal mild milk mill mix model money month mood mouth mud nail naked navy neat neck nerve nest noise
norm north note noun nurse oak odd oil okay onto oral organ oven owe own pace pack pad paint palm pan panel
panic park party paste pat patch pause peak pen pet phase phone photo pick pie pig pile pill pilot pin pink
pipe pit pitch pity plate plot plug plus poem poet pole poll pond pool pop pose pot pour pray prime prize
proof proud prove pump pure quiz rack rail range rank rare raw ray reach rear rely rent rich rid ring riot
rip risk rival rob robot rod role rope rough row rub ruin rush rust sack sad sake sale scan scene scent score
seal search sect seek self shade shake shame sheep sheet shelf shell shift shine shirt shock shoe shoot shore
shout shut sick sigh sight silk silly sin sink site skill skirt slave slide slight slope smart smell smoke
snap soap sock sofa solid solve sore soul soup sour spare spark spin spine spite split spoil spoon sport
staff stage stair stake stamp steal steam steel steep steer stem stiff stir stock stone stool strip stuff
style sugar swear sweep sweet swim swing sword tale tank tap tape task taste tax tea tear teeth tend tent
text theme thief thumb tide tight tin tip toe toll tomb tongue tooth topic toss tough tour toy trace trail
trap tray treat trend trial trick trim troop trust truth tube tune twin twist ugly uncle union urban urge
usage user vain valid value van vast verb verse vest vice video vital void wage wake war ware warn waste
wax weak wealth weapon weave web wed weed weep weird wet whale wheat whip wine wing wipe wire wise wit wolf
worm wound wrap wrist yell yield zone
`

// EditDistance is the optimal-string-alignment distance (an adjacent
// transposition counts as one edit), the metric a spell corrector uses and the
// one the v2 per-token bound is stated in.
func EditDistance(a, b string) int {
	ra, rb := []rune(a), []rune(b)
	d := make([][]int, len(ra)+1)
	for i := range d {
		d[i] = make([]int, len(rb)+1)
		d[i][0] = i
	}
	for j := range d[0] {
		d[0][j] = j
	}
	for i := 1; i <= len(ra); i++ {
		for j := 1; j <= len(rb); j++ {
			cost := 1
			if ra[i-1] == rb[j-1] {
				cost = 0
			}
			best := d[i-1][j] + 1
			if d[i][j-1]+1 < best {
				best = d[i][j-1] + 1
			}
			if d[i-1][j-1]+cost < best {
				best = d[i-1][j-1] + cost
			}
			if i > 1 && j > 1 && ra[i-1] == rb[j-2] && ra[i-2] == rb[j-1] && d[i-2][j-2]+1 < best {
				best = d[i-2][j-2] + 1
			}
			d[i][j] = best
		}
	}
	return d[len(ra)][len(rb)]
}
