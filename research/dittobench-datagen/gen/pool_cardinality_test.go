package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/internal/humandata"
	"github.com/ditto-assistant/dittobench-datagen/internal/poolcanary"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// poolLedgerAuditedThrough is the newest bench_version the pool ledgers in
// gen/, universe/, and catalog/ were audited against. A new contract can
// start drawing from a pool the ledgers call frozen, so a version bump has to
// re-audit them before this constant moves.
const poolLedgerAuditedThrough = protocol.BenchVersionV14

// TestPoolCardinalityCanary is the generator's pool-cardinality canary (#492,
// item 4). A closed pool small enough to copy into a dispatch table is a
// benchmark defect. See internal/poolcanary for the floor and the ratchet.
//
// Scope is the v13 scored contract and the newest supported contract (v14).
// Both use the same generator: generateV13WorldMemorySuite on the v8+ world
// path. Most of the pools #492 names (djSecondHops, declPrefDomains,
// consTopics, leafNouns) belong to the v5..v7 persona-plan generators.
// memory_v2.go hands v8+ to the world path before those generators run, so
// the small ones are exempt as frozen. Their v13 counterparts are in universe/,
// which has its own canary.
//
// The classification was audited by covering GenerateDatasetWithSurface at
// v13 and v14 across all three run sizes and several surface salts, then
// matching each reached pool against the emitted questions, answers, and
// seeded records.
func TestPoolCardinalityCanary(t *testing.T) {
	if newest := protocol.NewestSupportedBenchVersion(); newest > poolLedgerAuditedThrough {
		t.Fatalf("bench_version %d is supported but the pool ledgers were audited through v%d: re-audit the gen/, universe/, and catalog/ ledgers for the new contract, then raise poolLedgerAuditedThrough (#492)", newest, poolLedgerAuditedThrough)
	}
	poolcanary.Check(t, ".", poolcanary.Ledger{
		Scored: []poolcanary.Pool{
			// Composed answer surfaces are measured by their product: a fixed
			// table can only cover the answer if it covers every combination.
			{Name: "humandata surnames × v12BankSuffixes", Parts: []string{"humandata.AllSurnames", "v12BankSuffixes"},
				Size: len(humandata.AllSurnames()) * len(v12BankSuffixes), Role: "answer (parser-divergence bank)"},
			{Name: "humandata surnames × v12ProjectSuffixes", Parts: []string{"humandata.AllSurnames", "v12ProjectSuffixes"},
				Size: len(humandata.AllSurnames()) * len(v12ProjectSuffixes), Role: "question entity (parser-divergence project)"},

			// Record-quantity family (familycompiler_v13.go).
			{Name: "familyV2Conventions", Size: len(familyV2Conventions), Role: "answer (stated sign convention)"},
			{Name: "FamilyV2DirectionAccept", Size: len(FamilyV2DirectionAccept), Role: "accepted answer forms"},
			{Name: "FamilyV2Currencies", Size: len(FamilyV2Currencies), Role: "question cue (unit)"},
			{Name: "FamilyV2Units", Size: len(FamilyV2Units), Role: "question cue (unit)"},
			{Name: "FamilyV2Cues", Size: len(FamilyV2Cues), Role: "question and record cue"},
			{Name: "familyV2Openers", Size: len(familyV2Openers), Role: "question template"},

			// World integrity (memory_v2.go v8WorldIntegrityCases).
			{Name: "declarativeAckForms", Size: len(declarativeAckForms), Role: "accepted answer forms"},

			// v13 injection envelope (injection_v13.go). A cue bank is measured on
			// its own: matching any one label already detects every envelope.
			{Name: "v13EnvelopeDelimiters", Size: len(v13EnvelopeDelimiters), Role: "record cue (injection envelope)"},
			{Name: "v13EnvelopeLabels", Size: len(v13EnvelopeLabels), Role: "record cue (injection envelope)"},
			{Name: "v13EnvelopeProvenance", Size: len(v13EnvelopeProvenance), Role: "record cue (injection envelope)"},
			{Name: "v13EnvelopeHandling", Size: len(v13EnvelopeHandling), Role: "record cue (injection envelope)"},

			// v13 stored-directive marker composition (v13_surface.go). The pass
			// runs on every v13 artifact but only fires on a pre-v13 fixed marker,
			// and the public envelopes plant none. One generator change would make
			// these live, so they stay on the ledger.
			{Name: "v13TagBrackets", Size: len(v13TagBrackets), Role: "record cue (directive marker)"},
			{Name: "v13TagPrefixes", Size: len(v13TagPrefixes), Role: "record cue (directive marker)"},
			{Name: "v13TagJoiners", Size: len(v13TagJoiners), Role: "record cue (directive marker)"},
			{Name: "v13TagSuffixes", Size: len(v13TagSuffixes), Role: "record cue (directive marker)"},
			{Name: "v13VerifiedAdjs", Size: len(v13VerifiedAdjs), Role: "record cue (directive marker)"},
			{Name: "v13VerifiedNouns", Size: len(v13VerifiedNouns), Role: "record cue (directive marker)"},
			{Name: "v13VerifiedCopulas", Size: len(v13VerifiedCopulas), Role: "record cue (directive marker)"},
			{Name: "v13ShouldReadCores", Size: len(v13ShouldReadCores), Role: "record cue (directive marker)"},
			{Name: "v13AlwaysReportLeads", Size: len(v13AlwaysReportLeads), Role: "record cue (directive marker)"},
			{Name: "v13AlwaysReportVerbs", Size: len(v13AlwaysReportVerbs), Role: "record cue (directive marker)"},
			{Name: "v13UseWheneverLeads", Size: len(v13UseWheneverLeads), Role: "record cue (directive marker)"},
			{Name: "v13UseWheneverTails", Size: len(v13UseWheneverTails), Role: "record cue (directive marker)"},
		},

		// Known-small scored pools when the canary landed (#492). Each value is
		// the exact current size. Growing a pool to the floor fails until its
		// entry is deleted, so this list only shrinks. These pools are frozen
		// into the v13/v14 known vectors: a fix ships with the next bench_version,
		// in the new contract's own bank.
		Allow: map[string]int{
			"familyV2Conventions":     3,  // three-valued by design; the record states which one applies
			"FamilyV2DirectionAccept": 8,  // synonyms for "unchanged"
			"FamilyV2Currencies":      4,  //
			"FamilyV2Units":           5,  //
			"FamilyV2Cues":            10, // cue never decides the answer: the record states the convention
			"familyV2Openers":         4,  //
			"declarativeAckForms":     16, // synonyms for an acknowledgment
			"v13EnvelopeDelimiters":   6,  //
			"v13EnvelopeLabels":       6,  //
			"v13EnvelopeProvenance":   5,  //
			"v13EnvelopeHandling":     4,  //
			"v13TagBrackets":          6,  // marker banks: not emitted at the public envelopes
			"v13TagPrefixes":          8,  //
			"v13TagJoiners":           5,  //
			"v13TagSuffixes":          7,  //
			"v13VerifiedAdjs":         6,  //
			"v13VerifiedNouns":        5,  //
			"v13VerifiedCopulas":      5,  //
			"v13ShouldReadCores":      5,  //
			"v13AlwaysReportLeads":    5,  //
			"v13AlwaysReportVerbs":    5,  //
			"v13UseWheneverLeads":     5,  //
			"v13UseWheneverTails":     4,  //
		},

		Exempt: map[string]string{
			// Configuration, report ordering, and match sets.
			"profilesV13":              poolcanary.Internal,
			"V13SlotOrder":             poolcanary.Internal,
			"v13InterimSlots":          poolcanary.Internal,
			"v13InterimGenerators":     poolcanary.Internal,
			"v13ComputedQuestionTypes": poolcanary.Internal,
			"v13InjectionMarkers":      poolcanary.Internal, // the fixed markers the pass looks for, not what it writes
			"knownAnswerKinds":         poolcanary.Internal, // mix-audit tooling

			// Tables keyed by a closed enum. Each value is a grammar or form bank.
			"v13DeclarativeAckGrammars": poolcanary.Keyed, // keyed by the three appearance settings
			"v13BehaviorGrammars":       poolcanary.Keyed,
			"familyV2ConventionForms":   poolcanary.Keyed, // keyed by convention

			// Frozen v2..v12 generators. The v5..v7 persona-plan families are
			// below. memory_v2.go sends v8+ to generateV8WorldMemorySuite before
			// any of them run.
			"Profiles": poolcanary.Frozen, "profilesV5": poolcanary.Frozen, "profilesV7": poolcanary.Frozen,
			"profilesV8": poolcanary.Frozen, "profilesV9": poolcanary.Frozen, "profilesV10": poolcanary.Frozen,
			"memoryTypeWeight": poolcanary.Frozen, "memoryTypeWeightV7": poolcanary.Frozen, "v7SaturatedRecallTypes": poolcanary.Frozen,
			"djLeaves": poolcanary.Frozen, "djSecondHops": poolcanary.Frozen, // djFirstHops (28) and djNames (52) clear the floor
			"declPrefDomains": poolcanary.Frozen, "declarativeAckSpecs": poolcanary.Frozen, "v8DeclarativeAckSpecs": poolcanary.Frozen,
			"greetingPrompts": poolcanary.Frozen, "confabSpecs": poolcanary.Frozen,
			"consTopics": poolcanary.Frozen, "mqDomains": poolcanary.Frozen,
			"relationPairs": poolcanary.Frozen, "leafNouns": poolcanary.Frozen, // relativeNames (40) clears the floor
			"tdNouns": poolcanary.Frozen, "nmSpecs": poolcanary.Frozen,
			"tcDomains": poolcanary.Frozen, "tcNumberWords": poolcanary.Frozen,
			"subFriends": poolcanary.Frozen, "subAttrs": poolcanary.Frozen,
			"convSpecs": poolcanary.Frozen, "convSpecsV7Extra": poolcanary.Frozen,
			"siFacts": poolcanary.Frozen, "siOverrides": poolcanary.Frozen, "siPrefs": poolcanary.Frozen,
			"ciAuthorityNotes": poolcanary.Frozen, "ciPayloadNotes": poolcanary.Frozen, "ciBenignConvention": poolcanary.Frozen,
			"ciBenignContexts": poolcanary.Frozen, "ciFacts": poolcanary.Frozen, "ciPrefs": poolcanary.Frozen,
			"familyBalanceOpeners":       poolcanary.Frozen, // v12 family compiler; v13 uses familyV2Openers
			"v11InjectionMarkerVariants": poolcanary.Frozen,
			"v12InjectionMarkers":        poolcanary.Frozen, "v12TagBrackets": poolcanary.Frozen, "v12TagPrefixes": poolcanary.Frozen,
			"v12TagJoiners": poolcanary.Frozen, "v12TagSuffixes": poolcanary.Frozen, "v12VerifiedAdjs": poolcanary.Frozen,
			"v12VerifiedNouns": poolcanary.Frozen, "v12VerifiedCopulas": poolcanary.Frozen, "v12ShouldReadCores": poolcanary.Frozen,
			"v12AlwaysReportLeads": poolcanary.Frozen, "v12AlwaysReportVerbs": poolcanary.Frozen,
			"v12UseWheneverLeads": poolcanary.Frozen, "v12UseWheneverTails": poolcanary.Frozen,
		},
	})
}
