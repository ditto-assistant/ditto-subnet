package grade

import (
	"regexp"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v14 reply-text reading (plainReplyText, numberPunctuationBounds,
// percentClaims). Chat models answer in light markdown, and top miner agents
// shipped final-writer typography passes (paired-`**` removal outside code
// spans, U+2011 folding) just to be graded on what the user reads; the
// operators allowed that as W8 typography (SN-342). From v14 the grader reads
// the reply the way it renders instead, so no harness needs the rewrite. Every
// rule here is request-independent and case-independent: it removes markup,
// never a digit, letter, sign, unit or word the model wrote. V13 and older
// grade byte-identically.

// ── Rendered markdown (plainReplyText) ──────────────────────────────────────

// PlainReplyTextV14 returns s as rendered text: the markup a chat model wraps
// an answer in is removed and the characters it marks up are kept.
//
//   - Non-ASCII space separators (no-break, thin, narrow no-break, figure and
//     ideographic spaces) read as an ASCII space, U+2028/U+2029 as a newline,
//     zero-width characters are dropped, and U+2212 MINUS SIGN reads as "-".
//   - Fenced code blocks lose their fence lines; their content is kept
//     verbatim. Inline code loses its backticks; its content is kept verbatim.
//   - ATX headings, blockquote markers, bullet markers (- * + •) and task
//     boxes are removed from the start of a line. Thematic breaks, setext
//     underlines and table delimiter rows are dropped. A table row loses its
//     outer pipes.
//   - Ordered-list ordinals ("1. ", "2) ") are removed only when the reply
//     holds a real numbered list: at least two item lines, numbered 1, 2, ...
//     (a nested list may restart at 1). A lone "5. That's the count." keeps
//     its number.
//   - Links and images read as their text ([text](url) -> text), autolinks
//     as their target.
//   - Paired `*`, `**`, `***`, `_`, `__`, `___` emphasis on one line is
//     removed when the opener follows a non-word character and precedes a
//     non-space, and the closer follows a non-space and precedes a non-word
//     character. Intraword markers (snake_case, 2*3*4) and spaced asterisks
//     (2 * 3) are text.
//
// Strikethrough (~~x~~) is deliberately kept: rendering it as deletion would
// let a reply strike out a distractor while still naming it.
func PlainReplyTextV14(s string) string {
	if s == "" {
		return s
	}
	s = replyTypographyV14(s)
	lines := strings.Split(s, "\n")
	type line struct {
		text string
		code bool
		drop bool
	}
	out := make([]line, 0, len(lines))
	fence := ""
	for _, raw := range lines {
		trimmed := strings.TrimLeft(raw, " \t")
		if fence != "" {
			if strings.HasPrefix(trimmed, fence) && strings.Trim(trimmed, fence[:1]+" \t") == "" {
				fence = ""
				continue
			}
			out = append(out, line{text: raw, code: true})
			continue
		}
		if f := openingFenceV14(trimmed); f != "" {
			fence = f
			continue
		}
		if breakOrDelimiterRowV14.MatchString(raw) {
			continue
		}
		out = append(out, line{text: leadingBlocksV14(raw)})
	}

	// Ordered-list ordinals: strip only a real numbered list.
	var items []int
	var ordinals []string
	for i, l := range out {
		if l.code {
			continue
		}
		if m := orderedMarkerV14.FindStringSubmatch(l.text); m != nil {
			items = append(items, i)
			ordinals = append(ordinals, m[2])
		}
	}
	if numberedListV14(ordinals) {
		for _, i := range items {
			out[i].text = orderedMarkerV14.ReplaceAllString(out[i].text, "$1")
		}
	}

	var b strings.Builder
	b.Grow(len(s))
	for i, l := range out {
		if i > 0 {
			b.WriteByte('\n')
		}
		if l.code {
			b.WriteString(l.text)
			continue
		}
		b.WriteString(inlineMarkdownV14(tableRowV14(l.text)))
	}
	return b.String()
}

// replyTypographyV14 maps the typographic code points that carry no meaning
// beyond their ASCII form.
func replyTypographyV14(s string) string {
	ascii := true
	for i := 0; i < len(s); i++ {
		if s[i] >= utf8.RuneSelf {
			ascii = false
			break
		}
	}
	if ascii {
		return s
	}
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case r == 0x200B || r == 0x200C || r == 0x200D || r == 0x2060 || r == 0xFEFF:
			// zero-width: drop
		case r == 0x2028 || r == 0x2029:
			b.WriteByte('\n')
		case r == '−':
			b.WriteByte('-')
		case r >= utf8.RuneSelf && unicode.IsSpace(r):
			b.WriteByte(' ')
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

// openingFenceV14 returns the fence ("```" or "~~~", at its full run length)
// that line opens, or "".
func openingFenceV14(trimmed string) string {
	for _, c := range []byte{'`', '~'} {
		n := 0
		for n < len(trimmed) && trimmed[n] == c {
			n++
		}
		if n >= 3 && (c != '`' || !strings.Contains(trimmed[n:], "`")) {
			return trimmed[:n]
		}
	}
	return ""
}

var (
	// A thematic break, a setext underline, or a table delimiter row: syntax
	// only, never a value.
	breakOrDelimiterRowV14 = regexp.MustCompile(`^[ \t]*(?:(?:[-*_][ \t]*){3,}|=+|\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?)[ \t]*$`)
	headingV14             = regexp.MustCompile(`^ {0,3}#{1,6}(?:[ \t]+|$)`)
	closingHashesV14       = regexp.MustCompile(`[ \t]+#+[ \t]*$`)
	blockquoteV14          = regexp.MustCompile(`^ {0,3}>[ \t]?`)
	bulletV14              = regexp.MustCompile(`^[ \t]*[-*+•][ \t]+(?:\[[ xX]\][ \t]+)?`)
	orderedMarkerV14       = regexp.MustCompile(`^([ \t]*)(\d{1,3})[.)][ \t]+`)
	imageOrLinkV14         = regexp.MustCompile(`!?\[([^\[\]\n]+)\]\([^()\s]*(?:\([^()\s]*\)[^()\s]*)*(?:[ \t]+"[^"\n]*")?\)`)
	autolinkV14            = regexp.MustCompile(`<([a-zA-Z][a-zA-Z0-9+.\-]*:[^<>\s]+|[^<>\s@]+@[^<>\s@]+)>`)
)

// leadingBlocksV14 strips the container and heading markers at the start of a
// line: nested blockquotes, then an ATX heading or a bullet.
func leadingBlocksV14(text string) string {
	for {
		next := blockquoteV14.ReplaceAllString(text, "")
		if next == text {
			break
		}
		text = next
	}
	if headingV14.MatchString(text) {
		text = headingV14.ReplaceAllString(text, "")
		text = closingHashesV14.ReplaceAllString(text, "")
		return text
	}
	return bulletV14.ReplaceAllString(text, "")
}

// numberedListV14 reports whether the ordinal lines form a numbered list: at
// least two items, the first numbered 1, each next one either one more than
// the previous or a nested restart at 1, and at least one item numbered 2.
func numberedListV14(ordinals []string) bool {
	if len(ordinals) < 2 {
		return false
	}
	prev, sawTwo := 0, false
	for _, ordinal := range ordinals {
		n, err := strconv.Atoi(ordinal)
		if err != nil || (n != prev+1 && n != 1) {
			return false
		}
		sawTwo = sawTwo || n == 2
		prev = n
	}
	return sawTwo
}

// tableRowV14 removes the outer pipes of a table row. Inner pipes stay as the
// cell separators they render as.
func tableRowV14(text string) string {
	trimmed := strings.TrimSpace(text)
	if len(trimmed) < 2 || trimmed[0] != '|' || strings.Count(trimmed, "|") < 2 {
		return text
	}
	trimmed = strings.TrimSpace(trimmed[1:])
	if strings.HasSuffix(trimmed, "|") {
		trimmed = strings.TrimSpace(trimmed[:len(trimmed)-1])
	}
	return trimmed
}

// inlineMarkdownV14 renders one line's inline markup: code spans keep their
// content verbatim; the text between them loses links and emphasis.
func inlineMarkdownV14(text string) string {
	if !strings.ContainsAny(text, "`*_[<") {
		return text
	}
	var b strings.Builder
	b.Grow(len(text))
	last := 0
	for i := 0; i < len(text); {
		if text[i] != '`' {
			i++
			continue
		}
		n := 0
		for i+n < len(text) && text[i+n] == '`' {
			n++
		}
		closeAt := closingBackticksV14(text, i+n, n)
		if closeAt < 0 {
			i += n
			continue
		}
		b.WriteString(inlineTextV14(text[last:i]))
		content := text[i+n : closeAt]
		if len(content) >= 2 && content[0] == ' ' && content[len(content)-1] == ' ' && strings.TrimSpace(content) != "" {
			content = content[1 : len(content)-1]
		}
		b.WriteString(content)
		i = closeAt + n
		last = i
	}
	b.WriteString(inlineTextV14(text[last:]))
	return b.String()
}

// closingBackticksV14 returns the index of the next backtick run of exactly n
// at or after from, or -1.
func closingBackticksV14(text string, from, n int) int {
	for j := from; j < len(text); {
		if text[j] != '`' {
			j++
			continue
		}
		k := j
		for k < len(text) && text[k] == '`' {
			k++
		}
		if k-j == n {
			return j
		}
		j = k
	}
	return -1
}

// inlineTextV14 renders links and emphasis in text that holds no code span.
func inlineTextV14(text string) string {
	if strings.ContainsAny(text, "[") {
		text = imageOrLinkV14.ReplaceAllString(text, "$1")
	}
	if strings.ContainsAny(text, "<") {
		text = autolinkV14.ReplaceAllString(text, "$1")
	}
	if strings.ContainsAny(text, "*_") {
		text = stripEmphasisV14(text)
	}
	return text
}

// stripEmphasisV14 removes paired emphasis delimiter runs (see
// PlainReplyTextV14). Nested pairs are removed outside in.
func stripEmphasisV14(text string) string {
	for i := 0; i < len(text); {
		c := text[i]
		if c != '*' && c != '_' {
			i++
			continue
		}
		n := 0
		for i+n < len(text) && text[i+n] == c {
			n++
		}
		if n > 3 || !emphasisOpenerV14(text, i, n) {
			i += n
			continue
		}
		closeAt := emphasisCloserV14(text, i+n, c, n)
		if closeAt < 0 {
			i += n
			continue
		}
		text = text[:i] + text[i+n:closeAt] + text[closeAt+n:]
		// Re-read from the same index: the content may open a nested pair.
	}
	return text
}

func emphasisOpenerV14(text string, at, n int) bool {
	c := text[at]
	if at > 0 {
		r, _ := utf8.DecodeLastRuneInString(text[:at])
		if isWordRuneV13(r) || r == rune(c) {
			return false
		}
	}
	if at+n >= len(text) {
		return false
	}
	r, _ := utf8.DecodeRuneInString(text[at+n:])
	return !unicode.IsSpace(r) && r != rune(c)
}

// emphasisCloserV14 finds the closing run of exactly n delimiters c at or
// after from, or -1.
func emphasisCloserV14(text string, from int, c byte, n int) int {
	for j := from; j < len(text); {
		if text[j] != c {
			j++
			continue
		}
		k := j
		for k < len(text) && text[k] == c {
			k++
		}
		if k-j == n && j > from {
			before, _ := utf8.DecodeLastRuneInString(text[:j])
			after := true
			if k < len(text) {
				r, _ := utf8.DecodeRuneInString(text[k:])
				after = !isWordRuneV13(r)
			}
			if !unicode.IsSpace(before) && after {
				return j
			}
		}
		j = k
	}
	return -1
}

// plainReply applies PlainReplyTextV14 to the graded reply fields.
func plainReply(resp protocol.RunResponse) protocol.RunResponse {
	resp.Answer = PlainReplyTextV14(resp.Answer)
	resp.FinalText = PlainReplyTextV14(resp.FinalText)
	return resp
}

// ReplyAsRead returns resp as every v14+ text grader reads it: the typographic
// hyphen fold (FoldReplyHyphens) and then the rendered-markdown reading
// (PlainReplyTextV14). Below v14 it returns resp unchanged. The scorer applies
// it to the text-graded tool cases, the result-usage check and the twin
// post-pass so the tool and memory axes read one reply the same way.
func ReplyAsRead(benchVersion int, resp protocol.RunResponse) protocol.RunResponse {
	policy := gradingPolicyForVersion(benchVersion)
	if policy.foldTypographicHyphens {
		resp = foldReplyHyphens(resp)
	}
	if policy.plainReplyText {
		resp = plainReply(resp)
	}
	return resp
}

// ReplyTextAsRead is ReplyAsRead for one reply string.
func ReplyTextAsRead(benchVersion int, text string) string {
	return ReplyAsRead(benchVersion, protocol.RunResponse{FinalText: text}).FinalText
}

// ── Sentence punctuation after a number (numberPunctuationBounds) ───────────

// numAttachedAfterV14 reports whether the rune at text[end:] attaches to a
// number token that ends at end. As in v13 a digit and "-" attach; a "." or
// "," attaches only when a digit follows it (a decimal or grouping
// separator). A "." or "," followed by anything else ends a sentence or a
// clause: "Your PIN is 4471, as saved" names 4471.
func numAttachedAfterV14(text string, end int) bool {
	r, size := utf8.DecodeRuneInString(text[end:])
	if r == '.' || r == ',' {
		next, _ := utf8.DecodeRuneInString(text[end+size:])
		return end+size < len(text) && unicode.IsDigit(next)
	}
	return numAttachedV13(r)
}

// ── Percent claims (percentClaims) ──────────────────────────────────────────

// percentClaimUnitV14 reports whether a quantity claim's unit is a percentage,
// for which "15%" states the value as "15 percent" does.
func percentClaimUnitV14(unit string) bool {
	switch strings.ToLower(strings.TrimSpace(unit)) {
	case "percent", "percentage points":
		return true
	}
	return false
}
