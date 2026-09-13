package protocol

import (
	"encoding/json"
	"strings"
	"testing"
)

// TestV13ClaimsAreGraderOnly: Claims never serialize (they are `json:"-"`),
// so they cannot reach the harness wire, the /seed payload, or the hashed
// artifact, and a claims key on input is ignored.
func TestV13ClaimsAreGraderOnly(t *testing.T) {
	mc := MemoryCase{
		BenchVersion: BenchVersionV13, ID: "c1", QuestionID: "c1", Question: "who?",
		ExpectedAnswer: "Dana Whitfield", AnswerKind: AnswerValue,
		Claims: []Claim{{Kind: ClaimKindPerson, Expected: "Dana Whitfield", Accept: []string{"Dana Whitfield"}, Critical: true, Weight: 1}},
	}
	raw, err := json.Marshal(mc)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(strings.ToLower(string(raw)), "claim") {
		t.Fatalf("claims reached JSON: %s", raw)
	}
	var back MemoryCase
	if err := json.Unmarshal([]byte(`{"id":"c1","claims":[{"Kind":"person","Expected":"x"}]}`), &back); err != nil {
		t.Fatal(err)
	}
	if len(back.Claims) != 0 {
		t.Fatalf("claims decoded from the wire: %+v", back.Claims)
	}
}
