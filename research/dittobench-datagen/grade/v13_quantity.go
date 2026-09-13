package grade

import (
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// v13 typed-quantity parsing. Every function here works on text already run
// through foldV13 and returns MENTIONS with byte positions, so the claim engine
// (v13_claims.go) can decide which mentions are asserted, rejected, superseded,
// or merely exposition. Nothing here decides a score.

// mention is one typed value found in a text span. key is the grader's identity
// for the value (two mentions with equal keys are the same asserted candidate);
// pos/end bound the mention inside the text it was extracted from.
type mention struct {
	key      string
	pos, end int
	// weakPast is stamped by the claim engine from the segment the mention sits
	// in; extractors leave it false.
	weakPast bool
}

// ---------------------------------------------------------------- money

// amountMention is a parsed monetary mention in integer minor units.
type amountMention struct {
	minor    int
	currency string // ISO code, "$"/"¥" for an ambiguous symbol, "" when unmarked
	explicit bool   // unit fixed by a decimal part, a currency mark, or a unit word
	bare     int    // the raw integer when no unit was marked (0 otherwise)
	pos, end int
}

var currencySymbols = map[string]string{
	"$": "$", "€": "EUR", "£": "GBP", "¥": "¥", "₹": "INR", "₩": "KRW", "₽": "RUB", "₺": "TRY", "r$": "BRL",
	"us$": "USD", "u$s": "USD", "ca$": "CAD", "c$": "CAD", "a$": "AUD", "au$": "AUD", "nz$": "NZD",
	"mx$": "MXN", "s$": "SGD", "hk$": "HKD", "chf": "CHF",
}

var currencyCodes = map[string]bool{
	"usd": true, "eur": true, "gbp": true, "cad": true, "aud": true, "jpy": true, "chf": true,
	"mxn": true, "brl": true, "nzd": true, "sgd": true, "hkd": true, "inr": true, "cny": true,
	"krw": true, "sek": true, "nok": true, "dkk": true, "pln": true, "czk": true, "zar": true,
}

// dollarCodes are the currencies an unqualified "$" is compatible with.
var dollarCodes = map[string]bool{"USD": true, "CAD": true, "AUD": true, "NZD": true, "SGD": true, "HKD": true, "MXN": true}

var majorUnitWordsEN = []string{"dollars", "dollar", "euros", "euro", "pounds", "pound", "bucks", "quid", "francs", "yen", "pesos", "reais", "rupees"}
var minorUnitWordsEN = []string{"cents", "cent", "minor units", "minor unit", "minor-units", "minor-unit", "pence", "penny", "centavos", "centimes", "céntimos", "centesimi"}

// currencyCompatible reports whether an amount marked with got can answer a
// question asked in want. Unmarked amounts and unknown requests are compatible
// with everything; an ambiguous symbol is compatible with its family.
func currencyCompatible(got, want string) bool {
	switch {
	case got == "" || want == "":
		return true
	case got == want:
		return true
	case got == "$":
		return dollarCodes[want]
	case got == "¥":
		return want == "JPY" || want == "CNY"
	}
	return false
}

// amountMentionsV13 extracts every monetary mention from folded text. unit is
// the requested AnswerUnit for a bare integer (AnswerUnitMinor reads it as
// minor units; anything else as whole major units). Percentages, clock times,
// ISO dates, and identifiers with attached digits are not amounts.
func amountMentionsV13(text, unit string, lex claimLexicon) []amountMention {
	var out []amountMention
	for _, tok := range numericTokensV13(text) {
		whole, frac, hasFrac, ok := parseAmountTokenV13(tok.text)
		if !ok {
			continue
		}
		cur, majorWord, minorWord := amountContext(text, tok.pos, tok.end, lex)
		if strings.HasPrefix(strings.TrimLeft(text[tok.end:], " "), "%") {
			continue
		}
		m := amountMention{currency: cur, pos: tok.pos, end: tok.end}
		switch {
		case hasFrac:
			m.minor, m.explicit = whole*100+frac, true
		case minorWord:
			m.minor, m.explicit = whole, true
		case cur != "" || majorWord:
			m.minor, m.explicit = whole*100, true
		default:
			m.bare = whole
			if unit == "minor" {
				m.minor = whole
			} else {
				m.minor = whole * 100
			}
		}
		out = append(out, m)
	}
	return out
}

// alternateMinor returns the reading of a bare integer under the OTHER unit,
// or -1 when the amount's unit was explicit. It lets the slot-equivalence check
// accept a canonical minor-unit slot beside a prose amount in ordinary units.
func (a amountMention) alternateMinor(unit string) int {
	if a.explicit || a.bare == 0 {
		return -1
	}
	if unit == "minor" {
		return a.bare * 100
	}
	return a.bare
}

// amountContext inspects the runes around a numeric token for a currency mark
// (symbol or code, before or after) and for major/minor unit words after it.
func amountContext(text string, pos, end int, lex claimLexicon) (currency string, majorWord, minorWord bool) {
	before := strings.TrimRight(text[:pos], " ")
	for sym, code := range currencySymbols {
		if strings.HasSuffix(before, sym) {
			// Longest symbol wins ("r$" over "$").
			if currency == "" || len(sym) > len(symbolFor(currency)) {
				currency = code
			}
		}
	}
	if currency == "" {
		if w := lastWord(before); currencyCodes[w] {
			currency = strings.ToUpper(w)
		}
	}
	after := strings.TrimLeft(text[end:], " ")
	for sym, code := range currencySymbols {
		if strings.HasPrefix(after, sym) && currency == "" {
			currency = code
		}
	}
	afterWords := leadingWords(after, 3)
	if len(afterWords) > 0 && currencyCodes[afterWords[0]] && currency == "" {
		currency = strings.ToUpper(afterWords[0])
	}
	// "411067 USD cents": a currency code between the amount and the unit word.
	if len(afterWords) > 1 && currencyCodes[afterWords[0]] {
		afterWords = afterWords[1:]
	}
	joined := strings.Join(afterWords, " ")
	for _, w := range lex.minorUnit {
		if joined == w || strings.HasPrefix(joined, w+" ") || strings.HasPrefix(joined, w+",") || strings.HasPrefix(joined, w+".") {
			minorWord = true
		}
	}
	// "in cents" / "en centavos" immediately BEFORE the amount also fixes minor units.
	if !minorWord {
		for _, w := range lex.minorUnit {
			for _, prep := range []string{"in ", "en ", "em ", "als ", "in "} {
				if strings.HasSuffix(before, prep+w) {
					minorWord = true
				}
			}
		}
	}
	if !minorWord {
		for _, w := range lex.majorUnit {
			if joined == w || strings.HasPrefix(joined, w+" ") || strings.HasPrefix(joined, w+",") || strings.HasPrefix(joined, w+".") {
				majorWord = true
			}
		}
	}
	return currency, majorWord, minorWord
}

func symbolFor(code string) string {
	for sym, c := range currencySymbols {
		if c == code {
			return sym
		}
	}
	return ""
}

func lastWord(s string) string {
	s = strings.TrimRight(s, " ")
	if i := strings.LastIndex(s, " "); i >= 0 {
		s = s[i+1:]
	}
	return strings.Trim(s, "(),;:")
}

func leadingWords(s string, n int) []string {
	fields := strings.Fields(s)
	if len(fields) > n {
		fields = fields[:n]
	}
	for i, f := range fields {
		fields[i] = strings.Trim(f, "(),;:!?\"'")
	}
	return fields
}

// numericToken is a run of digits with interior grouping/decimal separators.
type numericToken struct {
	text     string
	pos, end int
}

// numericTokensV13 scans folded text for numeric tokens. A token may carry
// interior "," "." "'" separators and space-grouped thousands ("4 110,67"),
// which are merged when every following group is exactly three digits. Tokens
// glued to a clock colon, an ISO-date hyphen, or a word character on either
// side ("order-42", "v13", "3rd") are identifiers, not quantities.
func numericTokensV13(text string) []numericToken {
	var out []numericToken
	i := 0
	for i < len(text) {
		r, size := utf8.DecodeRuneInString(text[i:])
		if !unicode.IsDigit(r) {
			i += size
			continue
		}
		start := i
		j := i
		for j < len(text) {
			rj, sj := utf8.DecodeRuneInString(text[j:])
			if unicode.IsDigit(rj) {
				j += sj
				continue
			}
			if (rj == ',' || rj == '.' || rj == '\'') && j+sj < len(text) {
				rn, _ := utf8.DecodeRuneInString(text[j+sj:])
				if unicode.IsDigit(rn) {
					j += sj
					continue
				}
			}
			break
		}
		// Space-grouped thousands: "4 110" / "4 110 000" / "4 110,67". Merge only
		// while the leading group is 1..3 digits, every following group is exactly
		// three digits, and the group is not glued to a word ("2019 500 people" is
		// two numbers because 2019 has four digits).
		for j+1 < len(text) && text[j] == ' ' {
			lead := text[start:j]
			if strings.ContainsAny(lead, ".,'") {
				break
			}
			if first := strings.Split(lead, " ")[0]; len(first) < 1 || len(first) > 3 {
				break
			}
			k := j + 1
			digits := 0
			for k < len(text) {
				rk, sk := utf8.DecodeRuneInString(text[k:])
				if !unicode.IsDigit(rk) {
					break
				}
				digits++
				k += sk
			}
			if digits != 3 || (k < len(text) && isWordRuneV13(peekRune(text, k))) {
				break
			}
			j = k
			if j+1 < len(text) && (text[j] == ',' || text[j] == '.') && unicode.IsDigit(peekRune(text, j+1)) {
				j++
				for j < len(text) && unicode.IsDigit(peekRune(text, j)) {
					_, sj := utf8.DecodeRuneInString(text[j:])
					j += sj
				}
			}
		}
		tok := text[start:j]
		glued := false
		if start > 0 {
			rp, _ := utf8.DecodeLastRuneInString(text[:start])
			if isWordRuneV13(rp) || rp == '-' || rp == ':' || rp == '/' {
				glued = true
			}
		}
		if j < len(text) {
			rn, _ := utf8.DecodeRuneInString(text[j:])
			if isWordRuneV13(rn) || rn == ':' || rn == '-' || rn == '/' {
				// "3rd", "10:30", "2026-03-04", "4/3/2026": not a quantity.
				glued = true
			}
		}
		if !glued {
			out = append(out, numericToken{text: strings.ReplaceAll(tok, " ", ""), pos: start, end: j})
		}
		i = j
		if i == start {
			i += size
		}
	}
	return out
}

func groupedSoFar(s string) bool {
	parts := strings.Split(s, " ")
	if len(parts) < 2 {
		return false
	}
	for _, p := range parts[1:] {
		if len(p) != 3 {
			return false
		}
	}
	return true
}

func peekRune(s string, i int) rune {
	if i >= len(s) {
		return ' '
	}
	r, _ := utf8.DecodeRuneInString(s[i:])
	return r
}

// parseAmountTokenV13 reads a numeric token as (whole, fraction, hasFraction).
// The LAST separator decides: exactly two digits after it is a decimal part
// ("4,110.67", "4.110,67", "4110.67"); three digits after it is thousands
// grouping ("1,500", "4.110", "1,234,567"); any other fractional width is
// rejected as neither a money rendering nor a whole number.
func parseAmountTokenV13(tok string) (whole, frac int, hasFrac bool, ok bool) {
	tok = strings.Trim(tok, ",.'")
	if tok == "" || len(tok) > 18 {
		return 0, 0, false, false
	}
	sep := strings.LastIndexAny(tok, ",.'")
	if sep < 0 {
		w, err := strconv.Atoi(tok)
		return w, 0, false, err == nil
	}
	tail := tok[sep+1:]
	switch {
	case len(tail) == 2 && tok[sep] != '\'':
		head := onlyDigits(tok[:sep])
		if head == "" || !allDigits(tail) {
			return 0, 0, false, false
		}
		w, err1 := strconv.Atoi(head)
		f, err2 := strconv.Atoi(tail)
		if err1 != nil || err2 != nil {
			return 0, 0, false, false
		}
		return w, f, true, true
	case len(tail) == 3:
		w, err := strconv.Atoi(onlyDigits(tok))
		return w, 0, false, err == nil
	}
	return 0, 0, false, false
}

// ---------------------------------------------------------------- number

// numberMentionsV13 extracts integer counts: whole numeric tokens (grouping
// separators allowed, no decimal part) plus spelled-out small numbers and the
// idiomatic once/twice.
func numberMentionsV13(text string, lex claimLexicon) []mention {
	var out []mention
	for _, tok := range numericTokensV13(text) {
		if strings.HasPrefix(strings.TrimLeft(text[tok.end:], " "), "%") {
			continue
		}
		whole, _, hasFrac, ok := parseAmountTokenV13(tok.text)
		if !ok || hasFrac {
			continue
		}
		out = append(out, mention{key: strconv.Itoa(whole), pos: tok.pos, end: tok.end})
	}
	for word, value := range lex.numberWords {
		for from := 0; ; {
			j := indexBoundedV13(text, word, from)
			if j < 0 {
				break
			}
			out = append(out, mention{key: strconv.Itoa(value), pos: j, end: j + len(word)})
			from = j + 1
		}
	}
	return out
}

// ---------------------------------------------------------------- dates

// dateMention is a calendar value with unspecified components left at zero.
type dateMention struct {
	y, m, d  int
	pos, end int
}

func (d dateMention) key() string {
	return strconv.Itoa(d.y) + "-" + strconv.Itoa(d.m) + "-" + strconv.Itoa(d.d)
}

// compatible reports whether two mentions could denote the same day: every
// component both specify is equal.
func (d dateMention) compatible(o dateMention) bool {
	return (d.y == 0 || o.y == 0 || d.y == o.y) &&
		(d.m == 0 || o.m == 0 || d.m == o.m) &&
		(d.d == 0 || o.d == 0 || d.d == o.d)
}

// parseExpectedDate reads the ISO expected value and its granularity
// ("day", "month", "year"); ok is false for anything else.
func parseExpectedDate(expected string) (dateMention, string, bool) {
	e := strings.TrimSpace(expected)
	parts := strings.Split(e, "-")
	var out dateMention
	switch len(parts) {
	case 3:
		y, e1 := strconv.Atoi(parts[0])
		m, e2 := strconv.Atoi(parts[1])
		d, e3 := strconv.Atoi(parts[2])
		if e1 != nil || e2 != nil || e3 != nil || len(parts[0]) != 4 || m < 1 || m > 12 || d < 1 || d > 31 {
			return out, "", false
		}
		return dateMention{y: y, m: m, d: d}, "day", true
	case 2:
		y, e1 := strconv.Atoi(parts[0])
		m, e2 := strconv.Atoi(parts[1])
		if e1 != nil || e2 != nil || len(parts[0]) != 4 || m < 1 || m > 12 {
			return out, "", false
		}
		return dateMention{y: y, m: m}, "month", true
	case 1:
		y, e1 := strconv.Atoi(parts[0])
		if e1 != nil || len(parts[0]) != 4 {
			return out, "", false
		}
		return dateMention{y: y}, "year", true
	}
	return out, "", false
}

var monthsEN = map[string]int{
	"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
	"august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
	"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

// dateMentionsV13 extracts calendar mentions: ISO forms, month-name forms with
// a day and/or year on either side (in English or the question language),
// day-only ordinals ("the 4th", "día 4", "le 4"), and numeric d/m/y or m/d/y
// only when the order is unambiguous. A bare month name counts only when it is
// a full name that is not also an ordinary word ("may" is excluded).
func dateMentionsV13(text string, lex claimLexicon) []dateMention {
	var out []dateMention
	// ISO yyyy-mm-dd / yyyy-mm.
	for i := 0; i+7 <= len(text); i++ {
		if !allDigits(text[i:i+4]) || text[i+4] != '-' || i+7 > len(text) || !allDigits(text[i+5:i+7]) {
			continue
		}
		if i > 0 && numAttachedV13(peekLastRune(text, i)) {
			continue
		}
		y, _ := strconv.Atoi(text[i : i+4])
		m, _ := strconv.Atoi(text[i+5 : i+7])
		end := i + 7
		d := 0
		if end+3 <= len(text) && text[end] == '-' && allDigits(text[end+1:end+3]) {
			d, _ = strconv.Atoi(text[end+1 : end+3])
			end += 3
		}
		if end < len(text) && numAttachedV13(peekRune(text, end)) {
			continue
		}
		if m >= 1 && m <= 12 && d <= 31 {
			out = append(out, dateMention{y: y, m: m, d: d, pos: i, end: end})
		}
	}
	// Month-name forms.
	words := wordSpans(text)
	for wi, w := range words {
		mon, ok := lex.months[w.text]
		if !ok {
			continue
		}
		dm := dateMention{m: mon, pos: w.pos, end: w.end}
		specified := false
		// Day before: "4 march", "4th of march", "4 de marzo", "4. märz".
		if wi > 0 {
			pi := wi - 1
			if words[pi].text == "of" || words[pi].text == "de" || words[pi].text == "do" || words[pi].text == "da" {
				pi--
			}
			if pi >= 0 {
				if d, ok := dayNumber(words[pi].text); ok {
					dm.d, dm.pos, specified = d, words[pi].pos, true
				}
			}
		}
		// Day after: "march 4", "march 4th", "march the 4th".
		ni := wi + 1
		if !specified && ni < len(words) {
			if words[ni].text == "the" || words[ni].text == "le" || words[ni].text == "el" || words[ni].text == "il" {
				ni++
			}
			if ni < len(words) {
				if d, ok := dayNumber(words[ni].text); ok {
					dm.d, dm.end, specified = d, words[ni].end, true
					ni++
				}
			}
		} else if specified {
			ni = wi + 1
		}
		// Year after: "march 4, 2026", "4 de marzo de 2026", "march 2026".
		if ni < len(words) {
			yi := ni
			if words[yi].text == "de" || words[yi].text == "of" || words[yi].text == "del" {
				yi++
			}
			if yi < len(words) && len(words[yi].text) == 4 && allDigits(words[yi].text) {
				y, _ := strconv.Atoi(words[yi].text)
				dm.y, dm.end, specified = y, words[yi].end, true
			}
		}
		if !specified {
			if len(w.text) < 4 || w.text == "may" {
				continue
			}
		}
		out = append(out, dm)
	}
	// Day-only ordinals: "the 4th", "on the 4th"; "día 4", "le 4", "il 4".
	for wi, w := range words {
		if d, ok := ordinalDay(w.text); ok {
			if wi > 0 && lex.months[words[wi-1].text] != 0 {
				continue // already consumed as "march 4th"
			}
			if wi+1 < len(words) && (lex.months[words[wi+1].text] != 0 || words[wi+1].text == "of" || words[wi+1].text == "de") {
				continue // consumed as "4th of march"
			}
			out = append(out, dateMention{d: d, pos: w.pos, end: w.end})
			continue
		}
		if wi > 0 && (words[wi-1].text == "día" || words[wi-1].text == "dia" || words[wi-1].text == "le" || words[wi-1].text == "il" || words[wi-1].text == "am") {
			if d, ok := dayNumber(w.text); ok && allDigits(strings.TrimSuffix(w.text, ".")) {
				if wi+1 < len(words) && lex.months[words[wi+1].text] != 0 {
					continue
				}
				out = append(out, dateMention{d: d, pos: w.pos, end: w.end})
			}
		}
	}
	// Numeric d/m/y, m/d/y (unambiguous only) and y/m/d.
	for _, w := range words {
		parts := strings.FieldsFunc(w.text, func(r rune) bool { return r == '/' || r == '.' })
		if len(parts) != 3 || !allDigits(parts[0]) || !allDigits(parts[1]) || !allDigits(parts[2]) {
			continue
		}
		a, _ := strconv.Atoi(parts[0])
		b, _ := strconv.Atoi(parts[1])
		c, _ := strconv.Atoi(parts[2])
		switch {
		case len(parts[0]) == 4 && b >= 1 && b <= 12 && c >= 1 && c <= 31:
			out = append(out, dateMention{y: a, m: b, d: c, pos: w.pos, end: w.end})
		case len(parts[2]) == 4 && a > 12 && a <= 31 && b >= 1 && b <= 12:
			out = append(out, dateMention{y: c, m: b, d: a, pos: w.pos, end: w.end})
		case len(parts[2]) == 4 && b > 12 && b <= 31 && a >= 1 && a <= 12:
			out = append(out, dateMention{y: c, m: a, d: b, pos: w.pos, end: w.end})
		}
	}
	return out
}

type wordSpan struct {
	text     string
	pos, end int
}

// wordSpans splits folded text into words with byte positions, trimming the
// punctuation a word is commonly wrapped in but keeping "4." (a German ordinal)
// distinguishable through dayNumber.
func wordSpans(text string) []wordSpan {
	var out []wordSpan
	i := 0
	for i < len(text) {
		if text[i] == ' ' {
			i++
			continue
		}
		j := strings.IndexByte(text[i:], ' ')
		if j < 0 {
			j = len(text)
		} else {
			j += i
		}
		raw := text[i:j]
		trimmed := strings.Trim(raw, "\"'(),;:!?¿¡«»[]")
		lead := strings.Index(raw, trimmed)
		if trimmed != "" && lead >= 0 {
			out = append(out, wordSpan{text: strings.TrimSuffix(trimmed, "."), pos: i + lead, end: i + lead + len(trimmed)})
		}
		i = j
	}
	return out
}

func allDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

// dayNumber reads "4", "04", "4th", "4º", "4°" as a day of month.
func dayNumber(w string) (int, bool) {
	w = strings.TrimSuffix(w, ".")
	for _, suf := range []string{"st", "nd", "rd", "th", "º", "°", "er", "e"} {
		if strings.HasSuffix(w, suf) && allDigits(strings.TrimSuffix(w, suf)) {
			w = strings.TrimSuffix(w, suf)
			break
		}
	}
	if !allDigits(w) || len(w) > 2 {
		return 0, false
	}
	d, _ := strconv.Atoi(w)
	if d < 1 || d > 31 {
		return 0, false
	}
	return d, true
}

// ordinalDay reads an English ordinal ("4th", "22nd") as a day of month.
func ordinalDay(w string) (int, bool) {
	for _, suf := range []string{"st", "nd", "rd", "th"} {
		if strings.HasSuffix(w, suf) {
			core := strings.TrimSuffix(w, suf)
			if allDigits(core) && len(core) <= 2 {
				d, _ := strconv.Atoi(core)
				if d >= 1 && d <= 31 {
					return d, true
				}
			}
		}
	}
	return 0, false
}

func peekLastRune(s string, i int) rune {
	if i <= 0 {
		return ' '
	}
	r, _ := utf8.DecodeLastRuneInString(s[:i])
	return r
}

// ---------------------------------------------------------------- duration

// durationMentionsV13 extracts every "<number> <unit>" duration as a day count.
func durationMentionsV13(text string) []mention {
	var out []mention
	fields := strings.Fields(text)
	pos := 0
	for i, f := range fields {
		start := strings.Index(text[pos:], f) + pos
		pos = start + len(f)
		unit, ok := durationUnits[strings.Trim(f, ".,;:!?")]
		if !ok || i == 0 {
			continue
		}
		prev := strings.Trim(fields[i-1], ".,;:!?")
		days := 0
		switch {
		case prev == "a" || prev == "an" || prev == "one":
			days = unit
		case isPureNumber(prev):
			if num, scale, ok := parseDecimalToken(prev); ok {
				days = (num * unit) / scale
			}
		default:
			if w, ok := wordToNumber[prev]; ok {
				days = w * unit
			}
		}
		if days > 0 {
			out = append(out, mention{key: strconv.Itoa(days), pos: start - len(fields[i-1]) - 1, end: pos})
		}
	}
	return out
}
