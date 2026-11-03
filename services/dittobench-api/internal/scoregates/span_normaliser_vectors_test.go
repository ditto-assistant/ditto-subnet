package scoregates

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
	"unicode/utf8"
)

// spanNormaliserVectorInputs is the shared cross-language input list for the
// published v13 claim-span normaliser. TestSpanNormaliserSharedVectors pins
// NormalizeSpan and SpanTokens over it in
// testdata/v13_span_normaliser_vectors.json, and the starter kit's local gate
// replay (miners/dittobench-starter-kit/scripts/rehearsal_gates.py) replays the
// same file in scripts/test_local_rehearsal.py, so a miner's `rehearse --gates`
// tokenizes exactly as the validator does. Use long-assigned code points only:
// the kit runs on the interpreter's own Unicode database.
var spanNormaliserVectorInputs = []string{
	// TestNormalizeSpanVectors.
	"$4,110.67",
	"**Answer:** $4,110.67",
	"ANSWER: 4110.67 dollars",
	"Final answer — 4110.67",
	"- $4,110.67\n- based on the ledger",
	"1. Lisbon\n2. Porto",
	"`4110.67`",
	"４１１０.６７",
	"Lisbon,  since\t2019.",
	"José",
	"Ōsaka",
	"Zürich",
	"Москва",
	"",
	// TestSpanTokensFoldEveryHonestRenderingToOneClaimToken.
	"4110.67",
	"4,110.67",
	"4110.67 dollars",
	"**Answer:** $4110.67",
	`{"answer": "4110.67"}`,
	`final_answer {"answer":"4110.67","unit":"USD"}`,
	"USD 4110.670",
	"411067",
	"411067 cents",
	"4110",
	"$41,106.70",
	// TestSpanTokensFoldDiacriticsAndKeepNonLatin.
	"Jose",
	"Osaka",
	"Zurich",
	"Ｔｏｋｙｏ",
	"tokyo",
	"東京都新宿区",
	"Ōsa",
	// Labels, markers, and folds a Python port gets wrong by default.
	"Result: 411067",
	"**Answer:** the total was $4,110.67 — Lisbon’s share.",
	"Ánswer: 1234",           // the mark strips before the label rule runs
	"fınal answer: 4110.67",  // dotless i is not an RE2 (?i) fold of i
	"ﬁnal answer: 4110.67",   // NFKC unfolds the ligature first
	"Ａｎｓｗｅｒ：４１１０.６７",         // fullwidth label, colon, and digits
	"\vAnswer: 4110.67",      // RE2 \s has no vertical tab
	"Answer:\u2003$4,110.67", // NFKC turns the em space into a space
	"İstanbul",               // NFD + mark strip, then simple lowercase
	"Straße",                 // sharp s stays one letter
	"ΣΟΦΙΑ Ελλάδα",           // Greek lowercases rune by rune; tonos folds
	"\u212Aelvin",            // Kelvin sign is canonically K
	"Café au lait",           // short words fall below the rune floor
	"٣٤٥٦",                   // non-ASCII digits are a token, not a number
	"x² + ½ and Ⅻ chapters",  // compatibility forms fold under NFKC
	"नमस्ते",                 // Devanagari vowel signs and virama are Mn
	"กรุงเทพมหานคร",          // Thai vowel marks are Mn
	"서울특별시",                  // Hangul decomposes and recomposes
	"Достопримечательностьдостопримечательность", // 42 runes, 84 bytes: over MaxValueTokenLen
	"-0.50 and -$1,000.00",
}

type spanNormaliserVector struct {
	In         string   `json:"in"`
	Normalized string   `json:"normalized"`
	Tokens     []string `json:"tokens"`
}

// spanTokenStrings restates the SpanTokens rule over token STRINGS (SpanTokens
// keeps only hashes) so the fixture can publish readable tokens. The test
// proves it equals SpanTokens hash for hash.
func spanTokenStrings(text string) []string {
	normalized := NormalizeSpan(text)
	seen := map[string]struct{}{}
	add := func(tok string) {
		if tok == "" || len(tok) > MaxValueTokenLen {
			return
		}
		seen[tok] = struct{}{}
	}
	for _, match := range numberPattern.FindAllString(normalized, -1) {
		add(CanonicalNumber(match))
	}
	for _, tok := range spanTokenPattern.FindAllString(normalized, -1) {
		if utf8.RuneCountInString(tok) < MinStringTokenLen {
			continue
		}
		asciiDigits := true
		for _, r := range tok {
			if r < '0' || r > '9' {
				asciiDigits = false
				break
			}
		}
		if asciiDigits {
			continue
		}
		add(tok)
	}
	out := make([]string, 0, len(seen))
	for tok := range seen {
		out = append(out, tok)
	}
	sort.Strings(out)
	return out
}

// TestSpanNormaliserSharedVectors pins the cross-language normaliser fixture.
// Regenerate with SCOREGATES_UPDATE_GOLDEN=1 only when the published
// NormalizeSpan/SpanTokens rule or the input list changes on purpose, and move
// the starter kit's rehearsal_gates.py port in the same commit.
func TestSpanNormaliserSharedVectors(t *testing.T) {
	vectors := make([]spanNormaliserVector, 0, len(spanNormaliserVectorInputs))
	seenInputs := map[string]struct{}{}
	for _, in := range spanNormaliserVectorInputs {
		if _, dup := seenInputs[in]; dup {
			t.Fatalf("duplicate vector input %q", in)
		}
		seenInputs[in] = struct{}{}
		normalized := NormalizeSpan(in)
		if again := NormalizeSpan(normalized); again != normalized {
			t.Errorf("NormalizeSpan is not idempotent on %q: %q vs %q", in, normalized, again)
		}
		tokens := spanTokenStrings(in)
		hashes, truncated := SpanTokens(in)
		if truncated {
			t.Fatalf("%q tripped truncation", in)
		}
		want := TokenSet{}
		for _, tok := range tokens {
			want.Add(tok)
		}
		if len(want) != len(hashes) || !want.Subset(hashes) {
			t.Fatalf("fixture tokens %q for %q disagree with SpanTokens", tokens, in)
		}
		vectors = append(vectors, spanNormaliserVector{In: in, Normalized: normalized, Tokens: tokens})
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(map[string]any{
		"min_string_token_len": MinStringTokenLen,
		"max_value_token_len":  MaxValueTokenLen,
		"vectors":              vectors,
	}); err != nil {
		t.Fatal(err)
	}
	got := buf.Bytes()
	path := filepath.Join("testdata", "v13_span_normaliser_vectors.json")
	if os.Getenv("SCOREGATES_UPDATE_GOLDEN") == "1" {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden (set SCOREGATES_UPDATE_GOLDEN=1 to write it): %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("v13 span-normaliser fixture drifted; the starter kit's rehearsal_gates.py port must move with it:\n%s\n--- want ---\n%s", got, want)
	}
}
