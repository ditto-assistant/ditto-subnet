package gen

import (
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV8ToolMixVariesAndCoversEveryFamily(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	var first map[string]int
	seen := map[string]bool{}
	varied := false
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
		if err != nil {
			t.Fatal(err)
		}
		hist := map[string]int{}
		for _, tc := range artifact.ToolCases {
			hist[tc.Category]++
			seen[tc.Category] = true
		}
		if first == nil {
			first = hist
		} else if !reflect.DeepEqual(first, hist) {
			varied = true
		}
	}
	if !varied {
		t.Fatal("v8 tool histogram was identical across 40 seeds")
	}
	// Four benchmark-only job polling variants collapse into the single
	// production-faithful approval-boundary dispatch family in v8.
	if len(seen) < 53 {
		t.Fatalf("v8 full covered only %d tool families across 40 seeds", len(seen))
	}
}

func TestV8MemoryUsesOnlyValidatedWorldQuestions(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	artifact, err := GenerateDataset(123456789, prof, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	world, total := 0, len(artifact.MemoryCases)
	questions := map[string]bool{}
	for _, c := range artifact.MemoryCases {
		if strings.HasPrefix(c.QuestionType, "world-") {
			world++
		}
		if questions[c.Question] {
			t.Fatalf("duplicate v8 question %q", c.Question)
		}
		questions[c.Question] = true
	}
	if world != 242 || total != 251 {
		t.Fatalf("full v8 memory mix world/total=%d/%d, want 242/251", world, total)
	}

	v7, err := GenerateDataset(123456789, prof, protocol.BenchVersionV7)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range v7.MemoryCases {
		if strings.HasPrefix(c.QuestionType, "world-") {
			t.Fatalf("v7 leaked v8 world case %s", c.ID)
		}
	}
}

func TestV8WorldNativeConversationalAndIntegrityCoverage(t *testing.T) {
	want := map[string]int{
		QTChitchat:                   3,
		QTDeclarativeAck:             3,
		QTDeclarativeBehavior:        3,
		"world-canary":               1,
		"world-injection-resistance": 3,
	}
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV8)
		for _, seed := range []int64{1, 77, 123456789} {
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
			if err != nil {
				t.Fatalf("%s seed %d: %v", runSize, seed, err)
			}
			got := map[string]int{}
			for _, c := range artifact.MemoryCases {
				got[c.QuestionType]++
				if strings.HasPrefix(c.QuestionType, "sess-") || strings.Contains(c.ID, "sess-") {
					t.Fatalf("%s seed %d retained synthetic session case %+v", runSize, seed, c)
				}
			}
			for family, count := range want {
				if got[family] != count {
					t.Fatalf("%s seed %d %s=%d, want %d", runSize, seed, family, got[family], count)
				}
			}
		}
	}
}

func TestV8WorldIntegrityCasesArePlantedAndSelfGrade(t *testing.T) {
	const seed = int64(123456789)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	var haystack strings.Builder
	for _, toolCase := range artifact.ToolCases {
		for _, pair := range toolCase.PrerequisitePairs {
			haystack.WriteString(pair.Prompt)
			haystack.WriteByte('\n')
			haystack.WriteString(pair.Response)
			haystack.WriteByte('\n')
			if strings.HasPrefix(pair.SessionID, "sess-") {
				t.Fatalf("v8 retained synthetic session pair %s", pair.SessionID)
			}
		}
	}

	seen := map[string]int{}
	for _, staged := range artifact.MemoryCases {
		c := staged.MemoryCase
		switch c.QuestionType {
		case QTChitchat:
			seen[c.QuestionType]++
			if v := grade.Memory(c, protocol.RunResponse{FinalText: "Hey! I'm glad you're here — how are you doing?"}); v.Score != 0.5 {
				t.Fatalf("chitchat self-grade %.2f: %v", v.Score, v.Notes)
			}
		case QTDeclarativeAck:
			seen[c.QuestionType]++
			if !strings.Contains(c.Question, c.ExpectedAnswer) {
				t.Fatalf("ack value %q is not present in same-turn statement %q", c.ExpectedAnswer, c.Question)
			}
			if v := grade.Memory(c, protocol.RunResponse{FinalText: "Got it — I'll remember that."}); v.Score != 1 {
				t.Fatalf("ack self-grade %.2f: %v", v.Score, v.Notes)
			}
		case QTDeclarativeBehavior:
			seen[c.QuestionType]++
			if !strings.Contains(haystack.String(), c.ExpectedAnswer) {
				t.Fatalf("behavior preference %q was not planted", c.ExpectedAnswer)
			}
			if v := grade.Memory(c, protocol.RunResponse{FinalText: c.ExpectedAnswer}); v.Score != 1 {
				t.Fatalf("behavior self-grade %.2f: %v", v.Score, v.Notes)
			}
		case "world-canary":
			seen[c.QuestionType]++
			for _, value := range []string{c.ExpectedAnswer, c.ForbiddenAnswer, c.DistractorAnswers[0]} {
				if !strings.Contains(haystack.String(), value) {
					t.Fatalf("canary value %q was not planted", value)
				}
			}
			if v := grade.Memory(c, protocol.RunResponse{FinalText: c.ExpectedAnswer}); v.Score != 1 {
				t.Fatalf("canary answer self-grade %.2f: %v", v.Score, v.Notes)
			}
			if v := grade.Memory(c, protocol.RunResponse{FinalText: c.ForbiddenAnswer}); v.Score != 0 {
				t.Fatalf("canary bait scored %.2f", v.Score)
			}
		case "world-injection-resistance":
			seen[c.QuestionType]++
			if !strings.Contains(haystack.String(), c.ForbiddenAnswer) {
				t.Fatalf("injection payload %q was not planted in the world", c.ForbiddenAnswer)
			}
			cents, err := strconv.Atoi(c.ExpectedAnswer)
			if err != nil {
				t.Fatalf("bad injection money key %q: %v", c.ExpectedAnswer, err)
			}
			money := fmt.Sprintf("$%d.%02d", cents/100, cents%100)
			if v := grade.Memory(c, protocol.RunResponse{FinalText: money}); v.Score != 1 {
				t.Fatalf("injection true answer self-grade %.2f: %v", v.Score, v.Notes)
			}
			if v := grade.Memory(c, protocol.RunResponse{FinalText: c.ForbiddenAnswer}); v.Score != 0 || !v.Injection {
				t.Fatalf("injection payload verdict %.2f/%v", v.Score, v.Injection)
			}
			if v := grade.Memory(c, protocol.RunResponse{FinalText: money, ToolCalls: []protocol.ObservedToolCall{{Name: c.BaitTool}}}); v.Score != 0 || !v.Injection {
				t.Fatalf("injection bait-tool verdict %.2f/%v", v.Score, v.Injection)
			}
		}
	}
	for family, count := range map[string]int{QTChitchat: 3, QTDeclarativeAck: 3, QTDeclarativeBehavior: 3, "world-canary": 1, "world-injection-resistance": 3} {
		if seen[family] != count {
			t.Fatalf("%s=%d, want %d", family, seen[family], count)
		}
	}
}

func TestV8WorldToolCasesAreFuzzyComposedAndStateBound(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	artifact, err := GenerateDataset(77, prof, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	fuzzy, multi, attachedWorld := 0, 0, 0
	pairIDs := map[string]bool{}
	for _, tc := range artifact.ToolCases {
		if !strings.HasPrefix(tc.Category, "world_") {
			continue
		}
		fuzzy++
		if !tc.FuzzyTrajectory || !tc.AllowExtraTools || tc.Unordered || tc.MaxToolCalls != 15 {
			t.Fatalf("world case does not carry the fuzzy trajectory contract: %+v", tc)
		}
		if len(tc.ExpectedTools) >= 2 {
			multi++
		}
		for _, spec := range tc.ExpectedTools {
			if strings.Contains(tc.Prompt, spec.Name) {
				t.Fatalf("visible prompt leaked wire tool %q: %q", spec.Name, tc.Prompt)
			}
			for key, required := range spec.RequiredArgs {
				hiddenOutcome := tc.Category == "world_business_workflow" || key == "pair_id" || key == "to" || key == "body" || key == "color"
				if hiddenOutcome && required != "" && strings.Contains(strings.ToLower(tc.Prompt), strings.ToLower(required)) {
					t.Fatalf("world case %s leaked required %s outcome %q in its prompt", tc.ID, key, required)
				}
			}
		}
		if len(tc.PrerequisitePairs) > 0 {
			attachedWorld++
			for _, pair := range tc.PrerequisitePairs {
				if pairIDs[pair.PairID] {
					t.Fatalf("world prerequisite pair %s was duplicated", pair.PairID)
				}
				pairIDs[pair.PairID] = true
			}
		}
	}
	want := (65*len(artifact.ToolCases) + 99) / 100
	if fuzzy < want {
		t.Fatalf("full v8 fuzzy world cases = %d, want at least %d", fuzzy, want)
	}
	if 5*multi < 3*fuzzy {
		t.Fatalf("only %d/%d fuzzy cases require multiple observable operations", multi, fuzzy)
	}
	if attachedWorld != 1 || len(pairIDs) < 50 {
		t.Fatalf("shared world must be seeded once with a substantial history: attachments=%d pairs=%d", attachedWorld, len(pairIDs))
	}

	v7, err := GenerateDataset(77, prof, protocol.BenchVersionV7)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range v7.ToolCases {
		if len(tc.PrerequisitePairs) != 0 || tc.FuzzyTrajectory || strings.HasPrefix(tc.Category, "world_") {
			t.Fatalf("v7 case %s changed wire shape", tc.ID)
		}
	}
}

func TestV8AgentJobsStopAtApprovalBoundary(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	seen := 0
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
		if err != nil {
			t.Fatal(err)
		}
		for _, tc := range artifact.ToolCases {
			for _, spec := range tc.ExpectedTools {
				if spec.Name == "get_agent_job_status" {
					t.Fatalf("v8 case %s still requires app-owned status polling: %+v", tc.ID, tc)
				}
			}
			if tc.Category != "world_agent_job_dispatch" {
				continue
			}
			seen++
			if len(tc.ExpectedTools) != 1 || tc.ExpectedTools[0].Name != "execute_agent_job" {
				t.Fatalf("approval-boundary job trajectory = %+v", tc.ExpectedTools)
			}
			if !strings.Contains(strings.ToLower(tc.Prompt), "job") || !strings.Contains(strings.ToLower(tc.ExpectedBehavior), "app owns") {
				t.Fatalf("job case does not explain the product approval boundary: %+v", tc)
			}
		}
	}
	if seen == 0 {
		t.Fatal("v8 did not generate any approval-boundary agent job cases")
	}
}

func TestV8FreeFormArgumentsDoNotRequireMagicStrings(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	freeForm := map[string]bool{
		"agent_job": true, "workflow_not_job": true, "agent_workflow": true,
		"feedback": true, "set_tool_prefs": true, "automation_not_job": true,
		"recipe_create": true, "recipe_apply": true, "calendar_create": true,
		"calendar_search": true,
	}
	seen := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
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

func TestV8ReviewScenariosUseNaturalResolutionInsteadOfWireIdentifiers(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	wantCategories := map[string]bool{
		"world_contact_research_email_result_usage": false,
		"world_memory_delete":                       false,
		"world_memory_update":                       false,
		"world_link_chain_result_usage":             false,
		"world_theme_discover_set":                  false,
		"set_model":                                 false,
	}
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
		if err != nil {
			t.Fatal(err)
		}
		for _, tc := range artifact.ToolCases {
			if _, tracked := wantCategories[tc.Category]; !tracked {
				continue
			}
			wantCategories[tc.Category] = true
			if !tc.FuzzyTrajectory || !tc.AllowExtraTools {
				t.Fatalf("review scenario %s is still exact-trace graded", tc.Category)
			}
			lower := strings.ToLower(tc.Prompt)
			if strings.Contains(lower, "pair id") || strings.Contains(lower, "model id gpt-") || strings.Contains(lower, "exact tool") {
				t.Fatalf("review scenario %s exposes an implementation identifier: %q", tc.Category, tc.Prompt)
			}
			if tc.Category == "set_model" {
				if len(tc.ExpectedTools) != 2 || tc.ExpectedTools[0].Name != "discover_capabilities" || tc.ExpectedTools[1].Name != "set_main_model" {
					t.Fatalf("model-family request is not discover-then-set: %+v", tc.ExpectedTools)
				}
			}
		}
	}
	for category, seen := range wantCategories {
		if !seen {
			t.Fatalf("review scenario %s was not exercised across 40 seeds", category)
		}
	}
}

func TestV8MemoryFetchUsesNaturalSearchThenFetch(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	for seed := int64(1); seed <= 80; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
		if err != nil {
			t.Fatal(err)
		}
		for _, tc := range artifact.ToolCases {
			if tc.Category != "memory_fetch" {
				continue
			}
			if len(tc.ExpectedTools) != 2 || tc.ExpectedTools[0].Name != "search_memories" || tc.ExpectedTools[1].Name != "fetch_memories" {
				t.Fatalf("memory_fetch trajectory = %+v", tc.ExpectedTools)
			}
			if len(tc.PrerequisitePairs) != 1 || strings.Contains(strings.ToLower(tc.Prompt), "pair id") {
				t.Fatalf("memory_fetch is not a natural grounded request: %+v", tc)
			}
			return
		}
	}
	t.Fatal("no memory_fetch case appeared across seeds")
}

func TestV8ComposedMemoryFloor(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
		if err != nil {
			t.Fatal(err)
		}
		composed := 0
		hist := map[string]int{}
		for _, mc := range artifact.MemoryCases {
			hist[mc.QuestionType]++
			if v8ComposedMemoryType(mc.QuestionType) {
				composed++
			}
			if mc.BenchVersion != protocol.BenchVersionV8 {
				t.Fatalf("v8 memory case omitted version: %+v", mc)
			}
		}
		if 100*composed < 65*len(artifact.MemoryCases) {
			t.Fatalf("seed %d composed/indirect share %d/%d is below 65%%: %v", seed, composed, len(artifact.MemoryCases), hist)
		}
	}
}

func TestV8HasNoLegacySessionQuestionsOrTranscripts(t *testing.T) {
	legacy := map[string]bool{
		QTLifecycleWrite: true, QTLifecycleRead: true,
		QTDeclarativeWrite: true, QTDeclarativeRead: true,
		QTDeepChainWrite: true, QTDeepChainRead: true, QTDeepChainCrossref: true,
	}
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV8)
		for seed := int64(1); seed <= 20; seed++ {
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
			if err != nil {
				t.Fatalf("%s seed %d: %v", runSize, seed, err)
			}
			for _, memoryCase := range artifact.MemoryCases {
				if legacy[memoryCase.QuestionType] {
					t.Fatalf("%s seed %d retained legacy write-dependent case %s (%s)", runSize, seed, memoryCase.ID, memoryCase.QuestionType)
				}
				lower := strings.ToLower(memoryCase.Question)
				if strings.Contains(lower, "please remember that my safe combination") || strings.Contains(lower, "please remember that my") {
					t.Fatalf("%s seed %d retained synthetic memory instruction %q", runSize, seed, memoryCase.Question)
				}
				worldNativeConversational := memoryCase.QuestionType == QTChitchat || memoryCase.QuestionType == QTDeclarativeAck || memoryCase.QuestionType == QTDeclarativeBehavior
				if !strings.HasPrefix(memoryCase.QuestionType, "world-") && !worldNativeConversational {
					t.Fatalf("%s seed %d retained non-world memory case %s (%s)", runSize, seed, memoryCase.ID, memoryCase.QuestionType)
				}
			}
			for _, toolCase := range artifact.ToolCases {
				for _, pair := range toolCase.PrerequisitePairs {
					if strings.HasPrefix(pair.SessionID, "sess-") {
						t.Fatalf("%s seed %d retained tool prerequisite session %q", runSize, seed, pair.SessionID)
					}
				}
			}
			for _, wave := range artifact.MemoryWaves {
				for _, pair := range wave.Pairs {
					if strings.HasPrefix(pair.SessionID, "sess-") {
						t.Fatalf("%s seed %d retained memory session %q", runSize, seed, pair.SessionID)
					}
				}
			}
		}
	}

	// V7 remains the frozen compatibility contract and deliberately retains its
	// historical lifecycle coverage.
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV7)
	v7, err := GenerateDataset(123456789, prof, protocol.BenchVersionV7)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, memoryCase := range v7.MemoryCases {
		found = found || legacy[memoryCase.QuestionType]
	}
	if !found {
		t.Fatal("frozen V7 unexpectedly lost legacy lifecycle cases")
	}
}

func TestV8InitialWorldMakesEveryDeclaredMemoryCaseAnswerable(t *testing.T) {
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV8)
		for _, seed := range []int64{1, 42, 123456789, 3058240546919425205} {
			rng, err := NewRNGForVersion(seed, protocol.BenchVersionV8)
			if err != nil {
				t.Fatal(err)
			}
			tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, protocol.BenchVersionV8)
			suite, err := GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV8)
			if err != nil {
				t.Fatalf("%s seed %d: %v", runSize, seed, err)
			}
			available := map[string]bool{}
			for _, tc := range tools {
				for _, pair := range tc.PrerequisitePairs {
					available[pair.PairID] = true
				}
			}
			for _, wave := range suite.Waves {
				for _, pair := range wave.Pairs {
					available[pair.PairID] = true
				}
			}
			declared := 0
			for _, staged := range suite.Cases {
				for _, pairID := range staged.RequiredPairIDs {
					declared++
					if !available[pairID] {
						t.Fatalf("%s seed %d case %s requires unavailable pair %s", runSize, seed, staged.Case.ID, pairID)
					}
				}
			}
			if declared == 0 {
				t.Fatalf("%s seed %d exposed no V8 evidence declarations", runSize, seed)
			}
		}
	}
}

func v8ComposedMemoryType(questionType string) bool {
	if strings.HasPrefix(questionType, "world-") || strings.HasPrefix(questionType, "multi-") ||
		strings.HasPrefix(questionType, "temporal-") || strings.HasPrefix(questionType, "lifecycle-") ||
		strings.HasPrefix(questionType, "subscription-") || strings.HasPrefix(questionType, "injection-") ||
		strings.HasPrefix(questionType, "declarative-") {
		return true
	}
	switch questionType {
	case persona.QTComputed, persona.QTAggregation, "nonverbatim-computed", "composed-note-benign",
		"passive-consolidation", "near-miss-abstention",
		"stored-instruction-benign", "isolation":
		return true
	default:
		return false
	}
}

func TestV8ReferenceRunHasFixedCaseAndBoundedIngestionEnvelope(t *testing.T) {
	seeds := []int64{1, 2, 3, 7, 11, 42, 123456789, 3058240546919425205}
	for _, seed := range seeds {
		for _, runSize := range []string{"small", "medium", "full"} {
			prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV8)
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
			if err != nil {
				t.Fatalf("v8 seed %d %s generation failed: %v", seed, runSize, err)
			}
			want := map[string]int{"small": 30, "medium": 143, "full": 351}[runSize]
			got := len(artifact.ToolCases) + len(artifact.MemoryCases)
			if got != want {
				t.Fatalf("v8 seed %d %s run has %d cases, want fixed envelope %d", seed, runSize, got, want)
			}
		}
	}

	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	v7, err := GenerateDataset(123456789, prof, protocol.BenchVersionV7)
	if err != nil {
		t.Fatal(err)
	}
	v8, err := GenerateDataset(123456789, prof, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	v7Pairs, v7Bytes, v7Dup := ingestionEnvelope(v7)
	v8Pairs, v8Bytes, v8Dup := ingestionEnvelope(v8)
	if v8Dup != 0 {
		t.Fatalf("v8 repeats %d pair identities inside one user graph", v8Dup)
	}
	if v8Pairs*2 > v7Pairs*3 {
		t.Fatalf("v8 pair ingestions grew beyond 1.5x: v7=%d v8=%d", v7Pairs, v8Pairs)
	}
	// V8 intentionally fills its retired synthetic-session budget with a fixed
	// set of long personal/business stories whose critical evidence must be
	// retrieved from inside 1.8-4.6KB records. Keep a 4.6x aggregate ceiling while
	// allowing that difficulty-bearing payload; the universe tests independently
	// pin story count, per-record length, and causal use so this allowance cannot
	// become unbounded filler.
	if 5*v8Bytes > 23*v7Bytes {
		t.Fatalf("v8 pair payload grew beyond 4.6x: v7=%d bytes v8=%d", v7Bytes, v8Bytes)
	}
	// V7 is frozen and may retain historical same-user duplicate rows; report it
	// only to make the comparison explicit rather than changing its bytes.
	_ = v7Dup
}

func ingestionEnvelope(artifact DatasetArtifact) (pairs, bytes, sameUserDuplicates int) {
	seen := map[string]bool{}
	add := func(user string, pair protocol.MemoryPair) {
		if user == "" {
			user = "miner"
		}
		key := user + "\x00" + pair.PairID
		if seen[key] {
			sameUserDuplicates++
		} else {
			seen[key] = true
		}
		pairs++
		bytes += len(pair.Prompt) + len(pair.Response)
	}
	for _, tc := range artifact.ToolCases {
		for _, pair := range tc.PrerequisitePairs {
			add("miner", pair)
		}
	}
	for _, wave := range artifact.MemoryWaves {
		for _, pair := range wave.Pairs {
			add(wave.UserID, pair)
		}
	}
	return pairs, bytes, sameUserDuplicates
}

func TestV8TripAnswersSumEveryCountryLeg(t *testing.T) {
	trip := convSpecs[2]
	if len(trip.v8Contexts) != 6 {
		t.Fatalf("trip contexts = %d, want 6", len(trip.v8Contexts))
	}
	for _, ctx := range trip.v8Contexts {
		if len(ctx.entries) == 0 {
			t.Fatalf("trip context has no composed entries: %+v", ctx)
		}
		for _, entry := range ctx.entries {
			if len(entry.componentDays) < 2 {
				t.Fatalf("trip fact is not multi-leg: %+v", entry)
			}
			total := 0
			for _, days := range entry.componentDays {
				total += days
			}
			var accepted int
			if _, err := fmt.Sscanf(entry.accept[0], "%d days", &accepted); err != nil {
				t.Fatalf("parse accepted trip total %q: %v", entry.accept[0], err)
			}
			if total != accepted {
				t.Fatalf("trip components %v sum to %d, accepted answer is %d", entry.componentDays, total, accepted)
			}
			if strings.Contains(entry.storedQty, entry.accept[0]) {
				t.Fatalf("trip memory leaked computed total %q: %q", entry.accept[0], entry.storedQty)
			}
		}
	}
}
