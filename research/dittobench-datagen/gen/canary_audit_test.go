package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestCanaryIsNeverAuditDuplicated pins the invariant the canary's scoring
// rests on: a run carries EXACTLY ONE canary case. The transform audit copies
// every grading field onto its sibling, including Forbidden (the bait nonce),
// so an audited canary would put the same nonce/bait pair in the suite twice.
// The scorer's canary disqualifier is charged per leaking case, so a single
// leaking behaviour would be penalised twice (0.5 -> 0.25) on the ~20% of seeds
// whose per-case audit draw happened to select the canary — a run-to-run
// scoring lottery unrelated to anything the harness did.
//
// The sweep is wide on purpose: the bug reproduced on roughly one seed in five,
// so a handful of seeds would have gone green over a live defect.
func TestCanaryIsNeverAuditDuplicated(t *testing.T) {
	prof, _ := ProfileFor("full")
	canaryBearing := 0
	for seed := int64(1); seed <= 250; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV4)
		if err != nil {
			t.Fatalf("seed %d: generate: %v", seed, err)
		}
		var canaries []string
		for _, c := range artifact.MemoryCases {
			if c.QuestionType == persona.QTCanary {
				canaries = append(canaries, c.QuestionID)
			}
		}
		if len(canaries) > 1 {
			t.Fatalf("seed %d: %d canary cases in one run (%s), want at most 1 — an audited canary double-charges the scorer's per-case integrity disqualifier",
				seed, len(canaries), strings.Join(canaries, ", "))
		}
		if len(canaries) == 1 {
			canaryBearing++
		}
	}
	// Guard the guard: if the canary stopped being emitted at all, the assertion
	// above would pass vacuously and the run would lose its integrity probe.
	if canaryBearing == 0 {
		t.Fatal("no seed produced a canary case — the integrity probe is not reaching the wire")
	}
	t.Logf("canary uniqueness: %d/250 seeds carry exactly one canary case", canaryBearing)
}

// TestInjectionIsNeverAudited re-pins the OLDER audit exclusion alongside the
// new canary one, so the added guard is shown not to have displaced it. An
// audited injection case gets an inert closer appended after the attack text,
// which is precisely the shape a trailing-question stripper answers correctly
// by accident.
func TestInjectionIsNeverAudited(t *testing.T) {
	prof, _ := ProfileFor("full")
	audited := 0
	for seed := int64(1); seed <= 250; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV4)
		if err != nil {
			t.Fatalf("seed %d: generate: %v", seed, err)
		}
		for _, c := range artifact.MemoryCases {
			if !strings.HasPrefix(c.QuestionID, persona.AuditCaseIDPrefix) {
				continue
			}
			audited++
			switch c.QuestionType {
			case persona.QTInjection:
				t.Fatalf("seed %d: audit case %s is an injection sibling — the attack text gains a benign trailing clause", seed, c.QuestionID)
			case persona.QTCanary:
				t.Fatalf("seed %d: audit case %s is a canary sibling — duplicates the nonce/bait pair", seed, c.QuestionID)
			}
		}
	}
	if audited == 0 {
		t.Fatal("no audit cases generated across the seed sweep — the exclusions are unproven")
	}
	t.Logf("audit exclusions: %d audit cases, none injection or canary", audited)
}

// TestDeleteInstructionsCarryAcknowledgeKind pins the AnswerKind on the two
// delete INSTRUCTION cases. Both say "forget my X"; neither is a question, so a
// correct response ("Done, I've removed that") names nothing and value
// containment on the noun phrase would grade phrasing rather than behaviour.
// Whether the delete actually persisted is graded by the paired read case,
// which cannot be faked.
func TestDeleteInstructionsCarryAcknowledgeKind(t *testing.T) {
	prof, _ := ProfileFor("full")
	// lc-del-w is the lifecycle delete (gen/lifecycle.go); xu-del-d is the
	// cross-user delete-under-A probe (gen/isolation.go).
	want := []string{"lc-del-w", "xu-del-d"}
	seen := map[string]int{}
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV4)
		if err != nil {
			t.Fatalf("seed %d: generate: %v", seed, err)
		}
		for _, c := range artifact.MemoryCases {
			for _, id := range want {
				if c.QuestionID != id {
					continue
				}
				seen[id]++
				if c.AnswerKind != protocol.AnswerAcknowledge {
					t.Fatalf("seed %d: case %s has AnswerKind %q, want %q — a delete instruction graded by value containment fails natural confirmations",
						seed, c.QuestionID, c.AnswerKind, protocol.AnswerAcknowledge)
				}
			}
		}
	}
	for _, id := range want {
		if seen[id] == 0 {
			t.Fatalf("case %s never appeared across the seed sweep — the assertion is vacuous", id)
		}
	}
	t.Logf("delete-instruction kinds: lc-del-w x%d, xu-del-d x%d", seen["lc-del-w"], seen["xu-del-d"])
}
