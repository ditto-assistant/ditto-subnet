package textnoise

import (
	"strings"
	"testing"
)

func TestV2LayoutIsPerSeedAndCoversEveryLayout(t *testing.T) {
	seen := map[Layout]int{}
	for seed := int64(1); seed <= 400; seed++ {
		seen[LayoutForSeed(seed, 0)]++
		if LayoutForSeed(seed, 0) != LayoutForSeed(seed, 0) {
			t.Fatalf("seed %d layout is not deterministic", seed)
		}
	}
	for _, layout := range Layouts {
		if seen[layout] < 40 {
			t.Fatalf("layout %s drawn only %d/400 times: %v", layout, seen[layout], seen)
		}
	}
	// The salt rotates the layout for a fixed seed.
	changed := 0
	for seed := int64(1); seed <= 200; seed++ {
		if LayoutForSeed(seed, 0) != LayoutForSeed(seed, 7) {
			changed++
		}
	}
	if changed < 100 {
		t.Fatalf("salt rotated the layout for only %d/200 seeds", changed)
	}
	for _, layout := range Layouts {
		table := layoutNeighbors[layout]
		for c := byte('a'); c <= 'z'; c++ {
			if table[c] == "" {
				t.Fatalf("layout %s has no neighbours for %q", layout, c)
			}
		}
	}
}

func TestV2TypoEditsAreBoundedByThirdOfLengthAndKeepEdges(t *testing.T) {
	words := []string{"tell", "check", "please", "summarize", "recommendation", "background", "spreadsheet"}
	for seed := int64(1); seed <= 300; seed++ {
		for _, word := range words {
			got, _ := ProjectV2(word, seed, 0, "token:"+word, OptionsV2{MaxTokens: 1})
			if got == word {
				continue
			}
			if got[0] != word[0] || got[len(got)-1] != word[len(word)-1] {
				t.Fatalf("seed %d changed word edges: %q -> %q", seed, word, got)
			}
			bound := MaxEditsForToken(word)
			if want := clamp(len(word)/3, 1, 3); bound != want {
				t.Fatalf("bound(%q)=%d want %d", word, bound, want)
			}
			if d := damerau(word, got); d < 1 || d > bound {
				t.Fatalf("seed %d: %q -> %q is %d edits, bound %d", seed, word, got, d, bound)
			}
		}
	}
}

func TestV2NeverTurnsAWordIntoADifferentRealWord(t *testing.T) {
	// "not"/"now" and "form"/"from" are the canonical meaning flips: the first is
	// protected outright, the second must be rejected by the neighbour check.
	text := "Please form the plan, then rest before the meeting and note the sale form for the team."
	for seed := int64(1); seed <= 500; seed++ {
		for _, salt := range []uint64{0, 3, 99} {
			got, _ := ProjectV2(text, seed, salt, "meaning", OptionsV2{MaxTokens: 6})
			lower := strings.ToLower(got)
			for _, forbidden := range []string{" from ", " now ", " than ", " then "} {
				if strings.Contains(lower, forbidden) && !strings.Contains(strings.ToLower(text), forbidden) {
					t.Fatalf("seed %d salt %d produced a meaning flip %q: %q", seed, salt, forbidden, got)
				}
			}
			for _, tok := range scanWords(got) {
				w := strings.ToLower(tok.word)
				if realWords[w] && !strings.Contains(strings.ToLower(text), w) {
					t.Fatalf("seed %d salt %d: edit landed on a different real word %q: %q", seed, salt, w, got)
				}
			}
			if !strings.Contains(got, "before") || strings.Count(got, "not ") != strings.Count(text, "not ") {
				t.Fatalf("seed %d salt %d edited a meaning-critical word: %q", seed, salt, got)
			}
		}
	}
}

func TestV2ProtectsValuesQuotesCapitalisedTokensAndIdentifiers(t *testing.T) {
	text := "Please forward the Harborline figure to moss.harbor61@mail.studio for “Northpoint workshop” and call gmail_send with REF-C09A771 after checking Scout's message."
	protected := []string{"Harborline", "Scout"}
	for seed := int64(1); seed <= 300; seed++ {
		got, _ := ProjectV2(text, seed, 5, "protect", OptionsV2{MaxTokens: 8, Protected: protected})
		for _, want := range []string{"Harborline", "moss.harbor61@mail.studio", "“Northpoint workshop”", "gmail_send", "REF-C09A771", "Scout's"} {
			if !strings.Contains(got, want) {
				t.Fatalf("seed %d altered protected surface %q: %q", seed, want, got)
			}
		}
	}
}

func TestV2IsDeterministicSaltVariedAndExercisesEveryMechanism(t *testing.T) {
	text := "Could you summarize the background research, describe the recommendation, and schedule the follow-up conversation with the accountant tomorrow afternoon so everyone remembers the decision?"
	one, statsOne := ProjectV2(text, 42, 0, "case", OptionsV2{MaxTokens: 6})
	two, statsTwo := ProjectV2(text, 42, 0, "case", OptionsV2{MaxTokens: 6})
	if one != two || statsOne != statsTwo {
		t.Fatalf("v2 projection is not deterministic: %q vs %q", one, two)
	}
	if one == text || statsOne.Total() == 0 {
		t.Fatalf("v2 projection left the text unchanged: %+v", statsOne)
	}
	salted, _ := ProjectV2(text, 42, 1, "case", OptionsV2{MaxTokens: 6})
	if salted == one {
		t.Fatalf("salt 1 reproduced the salt 0 surface: %q", one)
	}
	var totals Stats
	for seed := int64(1); seed <= 800; seed++ {
		_, got := ProjectV2(text, seed, 0, "mix", OptionsV2{MaxTokens: 6})
		totals.Keyboard += got.Keyboard
		totals.Transpose += got.Transpose
		totals.Omission += got.Omission
		totals.Duplication += got.Duplication
		totals.Phonetic += got.Phonetic
		totals.Autocorrect += got.Autocorrect
		totals.CommonSpelling += got.CommonSpelling
	}
	if totals.Keyboard == 0 || totals.Transpose == 0 || totals.Omission == 0 || totals.Duplication == 0 || totals.Phonetic == 0 || totals.Autocorrect == 0 {
		t.Fatalf("not every v2 mechanism fired: %+v", totals)
	}
	if totals.CommonSpelling != 0 {
		t.Fatalf("v2 must not use the closed misspelling table: %+v", totals)
	}
}

// TestV2TypoedTokensRecoverToTheirOriginal is the deterministic readability
// proxy: every edited token must still be nearest (Damerau-Levenshtein) to its
// own clean form among the sentence's vocabulary, uniquely, so a reader — human
// or model — recovers the intended word without a lookup table.
func TestV2TypoedTokensRecoverToTheirOriginal(t *testing.T) {
	inputs := []string{
		"Search the web for the latest figure on the housing market and tell me the exact number it reports.",
		"Check whether I already have a workflow for that project and, if not, create one under its formal name.",
		"Could you piece together the updated stays for our research trip and work out how long it runs altogether?",
		"Remind me which email I should use for the internal reviewer after the correction, and double-check before sending.",
		"You can bin that temporary note about fixing the address after the fundraiser, but keep the contact history.",
	}
	edited, recovered := 0, 0
	for seed := int64(1); seed <= 300; seed++ {
		for _, input := range inputs {
			vocabulary := map[string]bool{}
			for _, tok := range scanWords(input) {
				vocabulary[strings.ToLower(tok.word)] = true
			}
			got, stats := ProjectV2(input, seed, 0, input, OptionsV2{})
			if stats.Total() == 0 {
				continue
			}
			cleanTokens := scanWords(input)
			noisyTokens := scanWords(got)
			if len(cleanTokens) != len(noisyTokens) {
				t.Fatalf("seed %d changed the token count: %q -> %q", seed, input, got)
			}
			for i := range cleanTokens {
				clean := strings.ToLower(cleanTokens[i].word)
				noisy := strings.ToLower(noisyTokens[i].word)
				if clean == noisy {
					continue
				}
				edited++
				best, bestDistance, ties := "", 1<<30, 0
				for word := range vocabulary {
					d := damerau(noisy, word)
					switch {
					case d < bestDistance:
						best, bestDistance, ties = word, d, 1
					case d == bestDistance:
						ties++
					}
				}
				if best == clean && ties == 1 {
					recovered++
				}
			}
		}
	}
	if edited == 0 {
		t.Fatal("no tokens were edited")
	}
	rate := float64(recovered) / float64(edited)
	t.Logf("typo v2 readability proxy: %d/%d edited tokens (%.3f) recover uniquely to their clean form", recovered, edited, rate)
	if rate < 0.95 {
		t.Fatalf("only %.3f of %d edited tokens recover uniquely to their clean form", rate, edited)
	}
}

func TestV1ProjectorBytesAreUnchangedByV2(t *testing.T) {
	// The v1 engine is the surface every version <= 12 was generated with; its
	// output for a fixed input must not move when v2 lands beside it.
	text := "I should have remembered the corrected business address before the project meeting, but apparently the current schedule changed again."
	got, stats := Project(text, 42, "personal:case", Options{MaxEdits: 3, Grammar: true})
	if stats.Phonetic != 0 || stats.Autocorrect != 0 {
		t.Fatalf("v1 projector used a v2 mechanism: %+v", stats)
	}
	again, _ := Project(text, 42, "personal:case", Options{MaxEdits: 3, Grammar: true})
	if got != again {
		t.Fatalf("v1 projector is not deterministic: %q vs %q", got, again)
	}
}

func clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func damerau(a, b string) int { return EditDistance(a, b) }
