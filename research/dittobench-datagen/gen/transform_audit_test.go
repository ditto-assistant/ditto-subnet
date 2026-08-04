package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// auditCases splits a generated run into its transform-audit cases and the rest.
// Both halves of an audit pair share a TwinGroup, so the TRANSFORMED half is
// identified by its case-id prefix.
func auditCases(cases []protocol.MemoryCase) (audits, base []protocol.MemoryCase) {
	for _, c := range cases {
		if strings.HasPrefix(c.QuestionID, persona.AuditCaseIDPrefix) {
			audits = append(audits, c)
		} else {
			base = append(base, c)
		}
	}
	return audits, base
}

// TestRedTeamSurfaceBrittleParserFailsTransform is the headline Part A gate: a
// harness that answers by looking a question's EXACT surface form up in a table
// it built from the dataset must fail the transform audit, while the honest
// reference recovers both the base case and its transformed sibling.
//
// This is the concrete shape of the bulk of the rejected-harness corpus:
// template-fingerprint dispatchers and lookups keyed to the original phrasing.
// The audit's whole claim is that such a strategy is negative-EV under a
// post-commit rephrasing it could not have pre-handled. If this gate ever goes
// green for the brittle parser, that claim is false.
func TestRedTeamSurfaceBrittleParserFailsTransform(t *testing.T) {
	prof, _ := ProfileFor("full")
	var brittleTotal, honestTotal float64
	audited := 0
	for seed := int64(1); seed <= 25; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		audits, base := auditCases(cases)
		// The brittle strategy: memorize question text -> answer over every case
		// whose phrasing it has seen. It is given the base cases for free, which is
		// the most generous version of the attack.
		lookup := map[string]string{}
		for _, c := range base {
			lookup[c.Question] = c.ExpectedAnswer
		}
		for _, c := range audits {
			audited++
			brittle := lookup[c.Question] // exact-surface miss => empty
			brittleTotal += grade.Memory(c, protocol.RunResponse{FinalText: brittle, Answer: brittle}).Score
			honest := c.ExpectedAnswer
			honestTotal += grade.Memory(c, protocol.RunResponse{FinalText: honest, Answer: honest}).Score
		}
	}
	if audited == 0 {
		t.Fatal("no transform-audit cases generated across the seed sweep — the audit is not reaching the wire")
	}
	brittleMean := brittleTotal / float64(audited)
	honestMean := honestTotal / float64(audited)
	if honestMean < 0.99 {
		t.Fatalf("honest reference scored %.3f on audit cases, want ~1.0 — the transformed expected answer does not match the transformed question (a false-fail on honest miners)", honestMean)
	}
	if brittleMean > 0.10 {
		t.Fatalf("surface-brittle parser scored %.3f on %d audit cases, want <=0.10 — the transform did not defeat exact-surface lookup", brittleMean, audited)
	}
	t.Logf("transform audit: %d audited cases, brittle parser %.3f vs honest %.3f", audited, brittleMean, honestMean)
}

// TestRedTeamMemorizerFailsCovarianceTransform is the covariance gate. The
// invariance transform defeats a harness keyed to a question's SURFACE; only
// covariance defeats one that memorized the ANSWER. Here the transform asks for
// a state the memorized value is not: a prior state on an update chain, or a
// different point-in-time boundary. A harness replaying the base case's answer
// must score 0, while the honest reference scores full.
func TestRedTeamMemorizerFailsCovarianceTransform(t *testing.T) {
	prof, _ := ProfileFor("full")
	var memoTotal, honestTotal float64
	covariant := 0
	for seed := int64(1); seed <= 30; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		audits, base := auditCases(cases)
		byID := map[string]protocol.MemoryCase{}
		for _, c := range base {
			byID[c.QuestionID] = c
		}
		for _, c := range audits {
			b, ok := byID[strings.TrimPrefix(c.TwinGroup, persona.AuditTwinPrefix)]
			if !ok || b.ExpectedAnswer == c.ExpectedAnswer {
				continue // invariance transform: answer unchanged, covered elsewhere
			}
			covariant++
			memo := b.ExpectedAnswer // replay the memorized base answer
			memoTotal += grade.Memory(c, protocol.RunResponse{FinalText: memo, Answer: memo}).Score
			honest := c.ExpectedAnswer
			honestTotal += grade.Memory(c, protocol.RunResponse{FinalText: honest, Answer: honest}).Score
		}
	}
	if covariant == 0 {
		t.Fatal("no covariance transforms generated across the seed sweep — memorization is unpriced")
	}
	memoMean := memoTotal / float64(covariant)
	honestMean := honestTotal / float64(covariant)
	if honestMean < 0.99 {
		t.Fatalf("honest reference scored %.3f on covariance cases, want ~1.0", honestMean)
	}
	if memoMean > 0.05 {
		t.Fatalf("answer memorizer scored %.3f on %d covariance cases, want ~0 — the covariance transform does not make a memorized answer wrong", memoMean, covariant)
	}
	t.Logf("covariance audit: %d cases, memorizer %.3f vs honest %.3f", covariant, memoMean, honestMean)
}

// TestAuditPairsShareATwinGroup pins the pairing the whole metric rests on.
// The scorer derives transform robustness as the agreement rate WITHIN each
// audit group, so both halves must carry the same TwinGroup and the group must
// hold exactly two members. A group holding only the transform would look
// trivially self-consistent and measure nothing.
func TestAuditPairsShareATwinGroup(t *testing.T) {
	prof, _ := ProfileFor("full")
	pairs := 0
	for seed := int64(1); seed <= 20; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		groups := map[string][]protocol.MemoryCase{}
		for _, c := range cases {
			if strings.HasPrefix(c.TwinGroup, persona.AuditTwinPrefix) {
				groups[c.TwinGroup] = append(groups[c.TwinGroup], c)
			}
		}
		for g, members := range groups {
			if len(members) != 2 {
				t.Fatalf("seed %d: audit group %s has %d members, want exactly 2 (base + transform)", seed, g, len(members))
			}
			xf := 0
			for _, m := range members {
				if strings.HasPrefix(m.QuestionID, persona.AuditCaseIDPrefix) {
					xf++
				}
			}
			if xf != 1 {
				t.Fatalf("seed %d: audit group %s has %d transformed halves, want exactly 1", seed, g, xf)
			}
			pairs++
		}
	}
	if pairs == 0 {
		t.Fatal("no audit pairs formed")
	}
	t.Logf("audit pairing: %d well-formed base/transform pairs", pairs)
}

// TestTransformAuditCasesAreWireIndistinguishable pins the property the whole
// mechanism rests on: an audit case must look like any other case on the wire.
// If a harness can tell which cases are audited, it reasons only where it is
// watched and the audit measures nothing. The base/transform pairing rides
// TwinGroup, which ordinary metamorphic families already carry, so its presence
// is not itself a tell.
func TestTransformAuditCasesAreWireIndistinguishable(t *testing.T) {
	prof, _ := ProfileFor("full")
	for seed := int64(1); seed <= 10; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		audits, base := auditCases(cases)
		if len(audits) == 0 {
			continue
		}
		// Every field a harness can read must also occur on non-audit cases.
		types := map[string]bool{}
		for _, c := range base {
			types[c.QuestionType] = true
		}
		for _, c := range audits {
			if !types[c.QuestionType] {
				t.Errorf("seed %d: audit case %s has question_type %q that no ordinary case carries — that is a readable audit tell", seed, c.QuestionID, c.QuestionType)
			}
			if c.ExpectedAnswer == "" {
				t.Errorf("seed %d: audit case %s has no expected answer", seed, c.QuestionID)
			}
		}
	}
}

// TestTransformAuditIsReproducible is the trustless-scoring gate: selection, the
// transformed question text, and the transformed expected answer must all be
// pure functions of the public seed. A third party holding only the published
// seed regenerates the identical audit set, byte for byte — without that, no one
// can independently check an audit verdict.
func TestTransformAuditIsReproducible(t *testing.T) {
	prof, _ := ProfileFor("full")
	for seed := int64(1); seed <= 10; seed++ {
		_, a, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		_, b, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("regen: %v", err)
		}
		aa, _ := auditCases(a)
		bb, _ := auditCases(b)
		if len(aa) != len(bb) {
			t.Fatalf("seed %d: audit count drifted between regenerations: %d vs %d", seed, len(aa), len(bb))
		}
		for i := range aa {
			if aa[i].QuestionID != bb[i].QuestionID || aa[i].Question != bb[i].Question || aa[i].ExpectedAnswer != bb[i].ExpectedAnswer {
				t.Fatalf("seed %d: audit case %d did not regenerate identically", seed, i)
			}
		}
	}
}

// TestTransformSpaceIsNotAMemorizableLexicon guards the failure the pre-merge
// review flagged on the fixed injection pool: if a transform id always composed
// the same concrete surface, the transform space would collapse to a small
// enumerable lexicon a harness could pre-handle. The id->surface mapping is
// re-keyed per seed, so the SAME base question under the SAME transform id must
// produce different text across seeds.
func TestTransformSpaceIsNotAMemorizableLexicon(t *testing.T) {
	const transformID = 41
	seen := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		p := persona.BuildPlan(seed, personaOptsFor(40))
		for _, q := range persona.DeriveQuestions(p) {
			if q.Type != persona.QTSingleSession || q.Attribute != "city" {
				continue
			}
			if xf, _, ok := persona.Transform(p, q, transformID); ok {
				seen[xf.Text] = true
			}
			break
		}
	}
	if len(seen) < 5 {
		t.Fatalf("transform id %d produced only %d distinct surfaces across 40 seeds — the id->surface map is memorizable", transformID, len(seen))
	}
	t.Logf("transform id %d: %d distinct surfaces across 40 seeds", transformID, len(seen))
}

// TestTransformAuditRateIsInBudget checks the audit is a bounded share of the
// run and never inflates the case count. A run that silently grew would change
// what a miner pays per submission.
func TestTransformAuditRateIsInBudget(t *testing.T) {
	prof, _ := ProfileFor("full")
	for seed := int64(1); seed <= 15; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		audits, _ := auditCases(cases)
		if q := persona.AuditQuota(len(cases)); len(audits) > q {
			t.Errorf("seed %d: %d audit cases exceeds quota %d", seed, len(audits), q)
		}
	}
}
