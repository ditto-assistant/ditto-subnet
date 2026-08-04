package assistantvoice

import (
	"fmt"
	"strings"
	"testing"
)

func TestRenderReplacesColdAcknowledgementsDeterministically(t *testing.T) {
	got := Render(42, "pair-a", "trip-1", "", "The trip changed yesterday.", "Noted.")
	again := Render(42, "pair-a", "trip-1", "", "The trip changed yesterday.", "Noted.")
	if got != again {
		t.Fatalf("same identity rendered different responses: %q != %q", got, again)
	}
	if IsCold(got) {
		t.Fatalf("rendered response remains cold: %q", got)
	}
	if !strings.Contains(got, "state") && !strings.Contains(got, "changed") && !strings.Contains(got, "versions") && !strings.Contains(got, "update") && !strings.Contains(got, "before and after") {
		t.Fatalf("update acknowledgement lost its context: %q", got)
	}
}

func TestRenderPreservesSubstantiveFactsWhileWarmingPrefix(t *testing.T) {
	got := Render(42, "pair-b", "person", "", "My partner is Anika.", "Noted your partner, Anika.")
	if IsCold(got) {
		t.Fatalf("rendered response remains cold: %q", got)
	}
	if !strings.Contains(got, "partner, Anika.") {
		t.Fatalf("rendered response lost its factual suffix: %q", got)
	}

	direct := "Morgan Lee's office number is +1-212-555-0192."
	if got := Render(42, "pair-c", "accountant", "", "I need the number.", direct); got != direct {
		t.Fatalf("substantive direct answer changed: %q", got)
	}

	got = Render(42, "pair-d", "security", "", "My spare key code is 2194.", "Got it. Your spare key code is 2194.")
	if IsCold(got) || !strings.Contains(got, "Your spare key code is 2194.") {
		t.Fatalf("period-prefixed fact was not preserved and warmed: %q", got)
	}
}

func TestRenderKeepsThirdPartyAttributionWithoutExplainingScope(t *testing.T) {
	tests := []struct {
		response string
		values   []string
	}{
		{"Noted — that city is Tariq's, not yours.", []string{"city", "Tariq"}},
		{"Got it, Tariq's city, not something about you.", []string{"city", "Tariq"}},
		{"Understood — QP-7M4K2 is Tariq's code, not yours.", []string{"QP-7M4K2", "Tariq", "code"}},
		{"Noted — ZN-8P2C4 is Mina's code, kept separate from yours.", []string{"ZN-8P2C4", "Mina", "code"}},
	}
	for i, tt := range tests {
		got := Render(42, fmt.Sprintf("third-party-%d", i), "general", "", "Tariq told me this.", tt.response)
		lower := strings.ToLower(got)
		for _, banned := range []string{"not yours", "not something about you", "separate from yours"} {
			if strings.Contains(lower, banned) {
				t.Fatalf("rendered response exposes scope commentary: %q", got)
			}
		}
		for _, value := range tt.values {
			if !strings.Contains(got, value) {
				t.Fatalf("rendered response %q lost %q", got, value)
			}
		}
	}
}

func TestRenderVariesGenericAcknowledgementsByPair(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 80; i++ {
		pairID := "pair-" + strings.Repeat("x", i) + string(rune('a'+i%26))
		seen[Render(7, pairID, "general", "", "Here is another detail.", "Got it.")] = true
	}
	if len(seen) < 24 {
		t.Fatalf("generic acknowledgement diversity=%d, want at least 24", len(seen))
	}
}

func TestRenderUsesFirstNameOftenButNotAlways(t *testing.T) {
	const total = 500
	addressed := 0
	for i := 0; i < total; i++ {
		pairID := fmt.Sprintf("pair-%03d", i)
		response := Render(19, pairID, "general", "Peyton Spencer", "Here is another detail.", "Got it.")
		if strings.Contains(response, "Peyton") {
			addressed++
		}
		if strings.Contains(response, "Spencer") {
			t.Fatalf("reply used the full name instead of the conversational first name: %q", response)
		}
		if strings.HasPrefix(response, "Peyton, ") && !startsFirstPerson(strings.TrimPrefix(response, "Peyton, ")) {
			t.Fatalf("leading name produced an unnatural capitalized clause: %q", response)
		}
	}
	if addressed < total*20/100 || addressed > total*30/100 {
		t.Fatalf("name-address cadence=%d/%d, want 20-30%%", addressed, total)
	}
}

func TestNamePlacementDoesNotSplitASeededFactFromItsReply(t *testing.T) {
	const response = "Tidewater Instruments — good to know where you work."
	for i := 0; i < 100; i++ {
		got := addressUserByName(19, fmt.Sprintf("fact-%03d", i), "Peyton Spencer", response)
		if !strings.Contains(got, "Peyton") {
			continue
		}
		if strings.Contains(got, "Instruments, Peyton —") {
			t.Fatalf("name split factual subject from its acknowledgement: %q", got)
		}
		return
	}
	t.Fatal("test identities never selected name addressing")
}

func TestIsColdRejectsTransactionalSurfaces(t *testing.T) {
	for _, response := range []string{"Noted.", "Noted. Your safe code is 2194.", "Good to know.", "Got it. Your spare key code is 2194.", "Got it — size 10.", "Understood, that city belongs to Hana.", "Updated."} {
		if !IsCold(response) {
			t.Fatalf("cold response accepted: %q", response)
		}
	}
	for _, response := range []string{"Got you — I’ll remember that.", "Lovely — say hi to Luca.", "Morgan Lee's office number is 555-0192."} {
		if IsCold(response) {
			t.Fatalf("human response rejected: %q", response)
		}
	}
}
