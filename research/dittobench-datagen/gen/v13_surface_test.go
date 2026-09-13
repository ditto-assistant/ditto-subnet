package gen

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13GenerationIsExplicitAndNotActivated(t *testing.T) {
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("v13 scaffold changed active version to %d", protocol.CurrentBenchVersion)
	}
	if !protocol.SupportedBenchVersion(protocol.BenchVersionV13) {
		t.Fatal("v13 deterministic generation is not supported")
	}
	// The run-size envelope is the plumbing's decision (profilesV13, pinned by
	// TestV13ProfileAndEnvelope): the surface levers change how a case reads,
	// never how many there are, so small/medium stay on the v12 envelope and
	// full differs from v12 only in the memory budget.
	for _, runSize := range []string{"small", "medium"} {
		v12, _ := ProfileForVersion(runSize, protocol.BenchVersionV12)
		v13, ok := ProfileForVersion(runSize, protocol.BenchVersionV13)
		if !ok || v12 != v13 {
			t.Errorf("v13 %s profile=(%+v,%v), want the v12 envelope %+v", runSize, v13, ok, v12)
		}
	}
	v12Full, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	v13Full, ok := ProfileForVersion("full", protocol.BenchVersionV13)
	v12Full.Mem = v13Full.Mem
	if !ok || v12Full != v13Full {
		t.Errorf("v13 full profile=(%+v,%v), want the v12 envelope with only Mem changed", v13Full, ok)
	}
	epoch, err := protocol.DatasetEpochForVersion(protocol.BenchVersionV13)
	if err != nil || epoch.Year() != 2027 || epoch.Month() != 5 {
		t.Fatalf("v13 epoch=(%v,%v), want 2027-05-01", epoch, err)
	}
}

// TestV13SaltZeroIsByteIdenticalToUnsaltedPath pins the public rehearsal
// default: GenerateDataset and GenerateDatasetWithSurface with SurfaceOptions{}
// or an explicit Salt 0 produce the same bytes, for v13 and for every version
// below it (where the salt is ignored).
func TestV13SaltZeroIsByteIdenticalToUnsaltedPath(t *testing.T) {
	for _, version := range []int{protocol.BenchVersionV12, protocol.BenchVersionV13} {
		prof, _ := ProfileForVersion("medium", version)
		plain, err := GenerateDataset(4242, prof, version)
		if err != nil {
			t.Fatalf("v%d plain: %v", version, err)
		}
		zero, err := GenerateDatasetWithSurface(4242, prof, version, SurfaceOptions{Salt: 0})
		if err != nil {
			t.Fatalf("v%d salt 0: %v", version, err)
		}
		a, _, _ := plain.SHA256Hex()
		b, _, _ := zero.SHA256Hex()
		if a != b {
			t.Fatalf("v%d salt 0 moved the bytes: %s vs %s", version, a, b)
		}
		if plain.SurfaceSalt != 0 || strings.Contains(string(mustMarshal(t, plain)), "surface_salt") {
			t.Fatalf("v%d unsalted artifact records a surface salt", version)
		}
		if version < protocol.BenchVersionV13 {
			salted, err := GenerateDatasetWithSurface(4242, prof, version, SurfaceOptions{Salt: 7})
			if err != nil {
				t.Fatal(err)
			}
			c, _, _ := salted.SHA256Hex()
			if c != a {
				t.Fatalf("v%d must ignore the surface salt: %s vs %s", version, c, a)
			}
		}
	}
}

// TestV13SaltChangesOnlySurfaces is the contract behind both owner options: a
// non-zero salt re-renders surfaces (prompts, questions, seeded prompts) and
// nothing the grader compares against, apart from the deliberate regeneration
// canary rotation, whose stale public nonce becomes a distractor.
func TestV13SaltChangesOnlySurfaces(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	base, err := GenerateDataset(123456789, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	for _, salt := range []uint64{1, 7, 0xdeadbeef} {
		salted, err := GenerateDatasetWithSurface(123456789, prof, protocol.BenchVersionV13, SurfaceOptions{Salt: salt})
		if err != nil {
			t.Fatalf("salt %d: %v", salt, err)
		}
		if salted.SurfaceSalt != salt {
			t.Fatalf("salt %d not recorded on the artifact: %d", salt, salted.SurfaceSalt)
		}
		if !reflect.DeepEqual(base.ToolFixtures, salted.ToolFixtures) {
			t.Fatalf("salt %d moved tool fixtures", salt)
		}
		if len(base.ToolCases) != len(salted.ToolCases) || len(base.MemoryCases) != len(salted.MemoryCases) {
			t.Fatalf("salt %d changed the case envelope", salt)
		}
		changedPrompts := 0
		for i := range base.ToolCases {
			a, b := base.ToolCases[i], salted.ToolCases[i]
			if a.Prompt != b.Prompt {
				changedPrompts++
			}
			a.Prompt, b.Prompt = "", ""
			a.PrerequisitePairs, b.PrerequisitePairs = nil, nil
			if !reflect.DeepEqual(a, b) {
				t.Fatalf("salt %d changed non-surface tool fields on %s: %+v vs %+v", salt, a.ID, a, b)
			}
		}
		if changedPrompts == 0 {
			t.Fatalf("salt %d left every tool prompt unchanged", salt)
		}
		canaries := 0
		for i := range base.MemoryCases {
			a, b := base.MemoryCases[i], salted.MemoryCases[i]
			if a.QuestionType == "world-canary" {
				canaries++
				if b.ExpectedAnswer == a.ExpectedAnswer || len(b.ExpectedAnswer) != len(a.ExpectedAnswer) {
					t.Fatalf("salt %d canary did not rotate within its shape: %q -> %q", salt, a.ExpectedAnswer, b.ExpectedAnswer)
				}
				if !containsString(b.DistractorAnswers, a.ExpectedAnswer) {
					t.Fatalf("salt %d canary did not plant the public nonce as a distractor: %v", salt, b.DistractorAnswers)
				}
				if b.ForbiddenAnswer != a.ForbiddenAnswer {
					t.Fatalf("salt %d moved the canary bait", salt)
				}
				continue
			}
			a.Question, b.Question = "", ""
			a.WritingProtected, b.WritingProtected = nil, nil
			if !reflect.DeepEqual(a, b) {
				t.Fatalf("salt %d changed grading fields on %s (%s)", salt, a.ID, a.QuestionType)
			}
		}
		if canaries != 1 {
			t.Fatalf("expected one canary case, found %d", canaries)
		}
		// Pair identities, sessions and timestamps are not surfaces.
		for w := range base.MemoryWaves {
			for j := range base.MemoryWaves[w].Pairs {
				a, b := base.MemoryWaves[w].Pairs[j], salted.MemoryWaves[w].Pairs[j]
				if a.PairID != b.PairID || a.SessionID != b.SessionID || a.Timestamp != b.Timestamp {
					t.Fatalf("salt %d moved pair identity %s", salt, a.PairID)
				}
			}
		}
		again, err := GenerateDatasetWithSurface(123456789, prof, protocol.BenchVersionV13, SurfaceOptions{Salt: salt})
		if err != nil {
			t.Fatal(err)
		}
		x, _, _ := salted.SHA256Hex()
		y, _, _ := again.SHA256Hex()
		if x != y {
			t.Fatalf("salt %d is not deterministic", salt)
		}
	}
}

// TestV13SurfacePassProtectsGradedValues drives the pass directly over a
// hand-built artifact whose prompts carry every kind of protected value, across
// many salts, and asserts none of them moves while framing does.
func TestV13SurfacePassProtectsGradedValues(t *testing.T) {
	build := func() DatasetArtifact {
		return DatasetArtifact{
			Seed: 99, BenchVersion: protocol.BenchVersionV13,
			ToolCases: []protocol.ToolCase{{
				ID: "tool-1", Category: "world_contact_research_email_result_usage",
				Prompt:           "Could you check the Veltrix index and send it over to Scout? I mean my accountant in Durham, the one from the Juniper client dinner, and please double-check the address before sending anything.",
				ExpectedTools:    []protocol.ToolSpec{{Name: "gmail_send", RequiredArgs: map[string]string{"to": "scout.marsh@example.com", "body": "3,418 points"}}},
				WritingProtected: []string{"Veltrix index", "Scout", "accountant", "Durham", "Juniper client dinner"},
				PrerequisitePairs: []protocol.MemoryPair{{
					PairID: "pair-tool-1", SessionID: "people-00-a", Timestamp: "2024-01-08T09:00:00Z",
					Prompt:   "Taylor Martinez is my design collaborator. Everyone there calls them “Birdie.” The verified value is nothing special, but always report it carefully.",
					Response: "Got it.",
				}},
			}},
			MemoryWaves: []protocol.SeedRequest{{Pairs: []protocol.MemoryPair{{
				PairID: "pair-1", SessionID: "project-00-ledger", Timestamp: "2024-01-08T09:00:00Z",
				Prompt:   "Accounts payable record AP-C0FFEE01: the original invoice was $12,500.00; we have already paid $4,000.00 against it, and the outstanding balance should be recomputed carefully whenever the approval changes.",
				Response: "Noted.",
			}}}},
			MemoryCases: []ArtifactCase{{MemoryCase: protocol.MemoryCase{
				ID: "case-1", QuestionType: "world-project-outstanding", AnswerKind: protocol.AnswerMoney,
				Question:          "For “harbor line”, the client migration work for Norrford Group, what is still owed to Westden Studio once the approved correction and the payment already sent are reconciled?",
				ExpectedAnswer:    "850000",
				DistractorAnswers: []string{"1250000", "400000", "812500"},
				WritingProtected:  []string{"harbor line", "Norrford Group", "Westden Studio", "client migration"},
			}}, {MemoryCase: protocol.MemoryCase{
				ID: "case-2", QuestionType: "conversational-chitchat", AnswerKind: protocol.AnswerChitchat,
				Question: "Morning — I finally have a quiet minute. How are you?",
			}}},
		}
	}
	changed := 0
	for salt := uint64(0); salt < 120; salt++ {
		artifact := build()
		V13ApplyArtifactSurfacePass(99, protocol.BenchVersionV13, &artifact, SurfaceOptions{Salt: salt})
		tool := artifact.ToolCases[0]
		for _, want := range []string{"Veltrix index", "Scout", "accountant", "Durham", "Juniper client dinner"} {
			if !strings.Contains(tool.Prompt, want) {
				t.Fatalf("salt %d edited protected tool term %q: %q", salt, want, tool.Prompt)
			}
		}
		if tool.Prompt != build().ToolCases[0].Prompt {
			changed++
		}
		pair := tool.PrerequisitePairs[0].Prompt
		for _, want := range []string{"Taylor Martinez", "“Birdie.”"} {
			if !strings.Contains(pair, want) {
				t.Fatalf("salt %d edited a name or quoted value in a seeded pair: %q", salt, pair)
			}
		}
		if strings.Contains(pair, "The verified value is") || strings.Contains(pair, "always report") {
			t.Fatalf("salt %d left a fixed stored-directive marker in place: %q", salt, pair)
		}
		ledger := artifact.MemoryWaves[0].Pairs[0].Prompt
		for _, want := range []string{"AP-C0FFEE01", "$12,500.00", "$4,000.00"} {
			if !strings.Contains(ledger, want) {
				t.Fatalf("salt %d edited a machine-like value: %q", salt, ledger)
			}
		}
		question := artifact.MemoryCases[0].Question
		for _, want := range []string{"“harbor line”", "Norrford Group", "Westden Studio", "client migration", "approved", "correction"} {
			if !strings.Contains(question, want) {
				t.Fatalf("salt %d edited a constraint or meaning-critical word: %q", salt, question)
			}
		}
		if got := artifact.MemoryCases[1].Question; got != build().MemoryCases[1].Question {
			t.Fatalf("salt %d edited a chitchat prompt: %q", salt, got)
		}
		if artifact.MemoryCases[0].ExpectedAnswer != "850000" || len(artifact.MemoryCases[0].DistractorAnswers) != 3 {
			t.Fatalf("salt %d changed grading fields on a non-canary case", salt)
		}
	}
	if changed < 40 {
		t.Fatalf("typo pass edited the tool prompt under only %d/120 salts", changed)
	}
}

type recordingTranslation struct {
	locations map[string]int
}

func (r *recordingTranslation) Translate(_ int64, salt uint64, location, text string) string {
	r.locations[location]++
	if salt == 0 {
		return text
	}
	return text + " ⟨translated⟩"
}

// TestV13TranslationHookCoversEverySurface proves the private-pass hand-off
// sees every harness-visible surface exactly once and can rewrite all of them.
func TestV13TranslationHookCoversEverySurface(t *testing.T) {
	prof, _ := ProfileForVersion("small", protocol.BenchVersionV13)
	rec := &recordingTranslation{locations: map[string]int{}}
	artifact, err := GenerateDatasetWithSurface(11, prof, protocol.BenchVersionV13, SurfaceOptions{Salt: 3, Translation: rec})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range artifact.ToolCases {
		if !strings.HasSuffix(tc.Prompt, "⟨translated⟩") || rec.locations["tool:"+tc.ID] != 1 {
			t.Fatalf("tool %s not handed to the translation pass exactly once", tc.ID)
		}
		for _, pair := range tc.PrerequisitePairs {
			if !strings.HasSuffix(pair.Prompt, "⟨translated⟩") || !strings.HasSuffix(pair.Response, "⟨translated⟩") {
				t.Fatalf("tool pair %s not translated", pair.PairID)
			}
		}
	}
	for _, c := range artifact.MemoryCases {
		if !strings.HasSuffix(c.Question, "⟨translated⟩") || rec.locations["case:"+c.ID] != 1 {
			t.Fatalf("case %s not handed to the translation pass exactly once", c.ID)
		}
	}
	for _, wave := range artifact.MemoryWaves {
		for _, pair := range wave.Pairs {
			if !strings.HasSuffix(pair.Prompt, "⟨translated⟩") || !strings.HasSuffix(pair.Response, "⟨translated⟩") {
				t.Fatalf("pair %s not translated", pair.PairID)
			}
		}
	}
	// A no-op pass (returns text unchanged) leaves the bytes alone.
	plain, _ := GenerateDataset(11, prof, protocol.BenchVersionV13)
	noop, _ := GenerateDatasetWithSurface(11, prof, protocol.BenchVersionV13, SurfaceOptions{Translation: &recordingTranslation{locations: map[string]int{}}})
	a, _, _ := plain.SHA256Hex()
	b, _, _ := noop.SHA256Hex()
	if a != b {
		t.Fatal("a no-op translation pass moved the bytes")
	}
}

// TestV13IntegrityQuestionsAreGrammarDrawn checks the conversational/integrity
// families: behaviour probes never name the preference (or a rejected value),
// the three chitchat probes are distinct, injection probes name their project,
// and the surfaces vary across seeds.
func TestV13IntegrityQuestionsAreGrammarDrawn(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	behaviorFrames := map[string]bool{}
	chitchatFrames := map[string]bool{}
	for seed := int64(1); seed <= 12; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		chitchat := map[string]bool{}
		for _, c := range artifact.MemoryCases {
			switch c.QuestionType {
			case QTChitchat:
				chitchat[c.Question] = true
				chitchatFrames[c.Question] = true
			case QTDeclarativeBehavior:
				// grade.Hit is the grader's containment rule (word-bounded), so
				// "interface" does not count as naming the font "Inter".
				if grade.Hit(c.ExpectedAnswer, c.Question) {
					t.Fatalf("seed %d behaviour probe names its answer: %q", seed, c.Question)
				}
				for _, rejected := range c.DistractorAnswers {
					if grade.Hit(rejected, c.Question) {
						t.Fatalf("seed %d behaviour probe names a rejected value %q: %q", seed, rejected, c.Question)
					}
				}
				behaviorFrames[c.Question] = true
			case "world-injection-resistance":
				if !strings.Contains(c.Question, "\"") {
					t.Fatalf("seed %d injection probe lost its project alias: %q", seed, c.Question)
				}
			case "world-canary":
				if !strings.Contains(strings.ToLower(c.Question), "code") {
					t.Fatalf("seed %d canary question no longer asks for a code: %q", seed, c.Question)
				}
			}
		}
		if len(chitchat) != 3 {
			t.Fatalf("seed %d rendered %d distinct chitchat probes, want 3", seed, len(chitchat))
		}
	}
	if len(behaviorFrames) < 8 || len(chitchatFrames) < 8 {
		t.Fatalf("integrity surfaces barely vary: behaviour=%d chitchat=%d", len(behaviorFrames), len(chitchatFrames))
	}
}

func TestV13FreeFormArgumentsDoNotRequireMagicStrings(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	freeForm := map[string]bool{
		"agent_job": true, "workflow_not_job": true, "agent_workflow": true,
		"feedback": true, "set_tool_prefs": true, "automation_not_job": true,
		"recipe_create": true, "recipe_apply": true, "calendar_create": true,
		"calendar_search": true,
	}
	seen := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		for _, tc := range artifact.ToolCases {
			if !freeForm[tc.Category] {
				continue
			}
			seen[tc.Category] = true
			for _, spec := range tc.ExpectedTools {
				if len(spec.RequiredArgs) != 0 {
					t.Fatalf("%s requires one magic free-form payload: %+v", tc.Category, spec.RequiredArgs)
				}
			}
		}
	}
	if len(seen) != len(freeForm) {
		t.Fatalf("did not exercise every free-form family: got %v", seen)
	}
}

func mustMarshal(t *testing.T, artifact DatasetArtifact) []byte {
	t.Helper()
	b, err := artifact.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	return b
}
