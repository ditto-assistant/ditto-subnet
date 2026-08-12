package gen

import (
	"encoding/json"
	"regexp"
	"slices"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func projectionFixture() ([]protocol.ToolCase, []StagedCase, []protocol.SeedRequest) {
	tools := []protocol.ToolCase{
		{
			ID: "tool-family-email-0001", Category: "email", Prompt: "fetch project-04-ledger from project-04 for miner",
			ExpectedTools: []protocol.ToolSpec{{Name: "fetch_memories", RequiredArgs: map[string]string{"pairIds": "project-04-ledger", "filter": `{"pair_id":"project-04-origin"}`}}},
			PrerequisitePairs: []protocol.MemoryPair{
				{PairID: "project-04-ledger", SessionID: "project-04", Timestamp: "2026-01-01T12:00:00Z", Prompt: "later", Response: "later answer"},
				{PairID: "project-04-origin", SessionID: "project-04", Timestamp: "2026-01-01T10:00:00Z", Prompt: "first", Response: "first answer"},
			},
		},
		{ID: "tool-family-search-0002", Category: "search", Prompt: "find it", ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}}},
		{ID: "tool-family-update-0003", Category: "memory", Prompt: "change it", ExpectedTools: []protocol.ToolSpec{{Name: "update_memory", RequiredArgs: map[string]string{"pair_id": "trip-01-correction"}}}},
	}
	memory := []StagedCase{
		{Case: protocol.MemoryCase{ID: "memory-canary-0001", QuestionID: "canary", QuestionType: "canary", Question: "hello", ExpectedAnswer: "secret"}, RunAfterWave: 0, RequiredPairIDs: []string{"project-04-origin"}},
		{Case: protocol.MemoryCase{ID: "memory-project-0002", QuestionID: "project-ledger", QuestionType: "computed", Question: "what is the total?", ExpectedAnswer: "42"}, RunAfterWave: 1, RequiredPairIDs: []string{"project-04-ledger"}},
		{Case: protocol.MemoryCase{ID: "memory-trip-0003", QuestionID: "trip-correction", QuestionType: "temporal", Question: "where now?", ExpectedAnswer: "Oslo"}, RunAfterWave: 1, RequiredPairIDs: []string{"trip-01-correction"}},
		{Case: protocol.MemoryCase{ID: "memory-isolation-0004", QuestionID: "isolation", QuestionType: "isolation", Question: "which color?", ExpectedAnswer: "blue"}, UserID: SecondaryUser, RunAfterWave: 0, RequiredPairIDs: []string{"shared-label"}},
	}
	waves := []protocol.SeedRequest{
		{
			UserID: PrimaryUser, Wave: 7,
			Pairs: []protocol.MemoryPair{
				{PairID: "trip-01-correction", SessionID: "trip-01", Timestamp: "2026-02-03T12:00:00Z", Prompt: "correction", Response: "Oslo"},
				{PairID: "project-04-ledger", SessionID: "project-04", Timestamp: "2026-01-01T12:00:00Z", Prompt: "later", Response: "later answer"},
				{PairID: "empty-one", Timestamp: "2026-01-02T12:00:00Z", Prompt: "one", Response: "1"},
				{PairID: "project-04-origin", SessionID: "project-04", Timestamp: "2026-01-01T10:00:00Z", Prompt: "first", Response: "first answer"},
				{PairID: "empty-two", Timestamp: "2026-01-03T12:00:00Z", Prompt: "two", Response: "2"},
				{PairID: "shared-label", SessionID: "shared-session", Timestamp: "2026-01-04T12:00:00Z", Prompt: "primary", Response: "red"},
			},
			Subjects: []protocol.Subject{{ID: "project-subject", SubjectText: "Project", DescriptionText: "ledger"}, {ID: "trip-subject", SubjectText: "Trip", DescriptionText: "travel"}},
			Links:    []protocol.SubjectLink{{SubjectID: "project-subject", PairID: "project-04-origin"}, {SubjectID: "trip-subject", PairID: "trip-01-correction"}},
		},
		{
			UserID: SecondaryUser, Wave: 99,
			Pairs:    []protocol.MemoryPair{{PairID: "shared-label", SessionID: "shared-session", Timestamp: "2026-01-04T12:00:00Z", Prompt: "secondary", Response: "blue"}},
			Subjects: []protocol.Subject{{ID: "project-subject", SubjectText: "Other project", DescriptionText: "separate graph"}},
			Links:    []protocol.SubjectLink{{SubjectID: "project-subject", PairID: "shared-label"}},
		},
	}
	return tools, memory, waves
}

func mustProjection(t *testing.T, seed int64) *HarnessProjection {
	t.Helper()
	tools, memory, waves := projectionFixture()
	p, err := BuildHarnessProjection(seed, testBlindingKey(0x53), protocol.BenchVersionV9, tools, memory, waves)
	if err != nil {
		t.Fatalf("BuildHarnessProjection: %v", err)
	}
	return p
}

func testBlindingKey(fill byte) []byte { return slices.Repeat([]byte{fill}, 32) }

func TestHostileProjectionRejectsPreV9Versions(t *testing.T) {
	tools, memory, waves := projectionFixture()
	for _, version := range []int{0, protocol.BenchVersionV7, protocol.BenchVersionV8} {
		if _, err := BuildHarnessProjection(1, testBlindingKey(1), version, tools, memory, waves); err == nil {
			t.Fatalf("version %d unexpectedly accepted", version)
		}
	}
	if _, err := BuildHarnessProjection(1, testBlindingKey(1), protocol.BenchVersionV10, tools, memory, waves); err != nil {
		t.Fatalf("v10 did not inherit hostile projection: %v", err)
	}
}

func TestV9ProjectionRequiresIndependent256BitBlindingKey(t *testing.T) {
	tools, memory, waves := projectionFixture()
	for _, key := range [][]byte{nil, {}, {1}, make([]byte, 31), make([]byte, 33)} {
		if _, err := BuildHarnessProjection(537, key, protocol.BenchVersionV9, tools, memory, waves); err == nil {
			t.Fatalf("accepted blinding key length %d", len(key))
		}
	}
}

func TestV9ProjectionIsByteDeterministic(t *testing.T) {
	a := mustProjection(t, 537)
	b := mustProjection(t, 537)
	aBytes, err := json.Marshal(struct {
		Manifest HarnessProjectionManifest
		Tools    []protocol.ToolCase
		Memory   []StagedCase
		Waves    []protocol.SeedRequest
	}{a.Manifest, a.ToolCases, a.MemoryCases, a.Waves})
	if err != nil {
		t.Fatal(err)
	}
	bBytes, err := json.Marshal(struct {
		Manifest HarnessProjectionManifest
		Tools    []protocol.ToolCase
		Memory   []StagedCase
		Waves    []protocol.SeedRequest
	}{b.Manifest, b.ToolCases, b.MemoryCases, b.Waves})
	if err != nil {
		t.Fatal(err)
	}
	if string(aBytes) != string(bBytes) {
		t.Fatal("same seed did not produce byte-identical projection")
	}
}

func TestV9ProjectionSameDatasetSeedIsUnlinkableAcrossRuns(t *testing.T) {
	tools, memory, waves := projectionFixture()
	a, err := BuildHarnessProjection(537, testBlindingKey(0x11), protocol.BenchVersionV9, tools, memory, waves)
	if err != nil {
		t.Fatal(err)
	}
	b, err := BuildHarnessProjection(537, testBlindingKey(0x22), protocol.BenchVersionV9, tools, memory, waves)
	if err != nil {
		t.Fatal(err)
	}
	collect := func(p *HarnessProjection) []string {
		var out []string
		for _, x := range p.Manifest.Users {
			out = append(out, x.Wire)
		}
		for _, x := range p.Manifest.Cases {
			out = append(out, x.Wire)
		}
		for _, x := range p.Manifest.Pairs {
			out = append(out, x.Wire)
		}
		for _, x := range p.Manifest.Sessions {
			out = append(out, x.Wire)
		}
		for _, x := range p.Manifest.Subjects {
			out = append(out, x.Wire)
		}
		return out
	}
	setA := map[string]bool{}
	for _, value := range collect(a) {
		setA[value] = true
	}
	for _, value := range collect(b) {
		if setA[value] {
			t.Fatalf("capability %q repeated across independent seeds", value)
		}
	}
}

func TestV9EveryProjectedIdentifierIsUUIDShapedAndUnique(t *testing.T) {
	p := mustProjection(t, 537)
	seen := map[string]string{}
	check := func(kind, wire string) {
		t.Helper()
		if !uuidPattern.MatchString(wire) {
			t.Errorf("%s capability %q is not UUID-shaped", kind, wire)
		}
		if prior := seen[wire]; prior != "" {
			t.Errorf("%s capability repeats %s", kind, prior)
		}
		seen[wire] = kind
	}
	for _, x := range p.Manifest.Users {
		check("user", x.Wire)
	}
	for _, x := range p.Manifest.Cases {
		check("case", x.Wire)
	}
	for _, x := range p.Manifest.Pairs {
		check("pair", x.Wire)
	}
	for _, x := range p.Manifest.Sessions {
		check("session", x.Wire)
	}
	for _, x := range p.Manifest.Subjects {
		check("subject", x.Wire)
	}
}

func TestV9ScopedAliasesPreserveOnlyRealEquality(t *testing.T) {
	p := mustProjection(t, 537)
	findPair := func(user, internal string) string {
		for _, x := range p.Manifest.Pairs {
			if x.UserID == user && x.Internal == internal {
				return x.Wire
			}
		}
		return ""
	}
	primary := findPair(PrimaryUser, "shared-label")
	secondary := findPair(SecondaryUser, "shared-label")
	if primary == "" || secondary == "" || primary == secondary {
		t.Fatalf("cross-graph pair aliases correlated: primary=%q secondary=%q", primary, secondary)
	}
	var prereq, seeded string
	for _, c := range p.ToolCases {
		for _, pair := range c.PrerequisitePairs {
			if pair.Prompt == "first" {
				prereq = pair.PairID
			}
		}
	}
	for _, pair := range p.Waves[0].Pairs {
		if pair.Prompt == "first" {
			seeded = pair.PairID
		}
	}
	if prereq == "" || prereq != seeded {
		t.Fatalf("same primary pair lost equality: prerequisite=%q seeded=%q", prereq, seeded)
	}
}

func TestV9EmptySessionsBecomeUniqueCapabilities(t *testing.T) {
	p := mustProjection(t, 537)
	got := map[string]string{}
	for _, pair := range p.Waves[0].Pairs {
		if pair.Prompt == "one" || pair.Prompt == "two" {
			got[pair.Prompt] = pair.SessionID
		}
	}
	if got["one"] == "" || got["two"] == "" || got["one"] == got["two"] {
		t.Fatalf("empty session aliases must not invent grouping: %+v", got)
	}
}

func TestV9PermutationPreservesChronologyInsideSession(t *testing.T) {
	p := mustProjection(t, 537)
	var first, later int = -1, -1
	var session string
	for i, pair := range p.Waves[0].Pairs {
		switch pair.Prompt {
		case "first":
			first, session = i, pair.SessionID
		case "later":
			later = i
		}
	}
	if first < 0 || later < 0 || first >= later {
		t.Fatalf("dependency chronology broken: first=%d later=%d", first, later)
	}
	if p.Waves[0].Pairs[later].SessionID != session {
		t.Fatal("same session did not preserve equality")
	}
}

func TestV9PermutationKeepsMemoryWaveBarrier(t *testing.T) {
	p := mustProjection(t, 537)
	lastWave := -1
	seen := map[string]bool{}
	for _, sc := range p.MemoryCases {
		if sc.RunAfterWave < lastWave {
			t.Fatalf("wave %d ran after %d", sc.RunAfterWave, lastWave)
		}
		lastWave = sc.RunAfterWave
		if seen[sc.Case.ID] {
			t.Fatalf("duplicate projected case %q", sc.Case.ID)
		}
		seen[sc.Case.ID] = true
	}
	if len(seen) != 4 {
		t.Fatalf("got %d memory cases, want 4", len(seen))
	}
}

func TestV9ProjectionRemovesWaveAndSemanticIdentifiersFromSeedJSON(t *testing.T) {
	p := mustProjection(t, 537)
	for i, wave := range p.Waves {
		body, err := json.Marshal(wave)
		if err != nil {
			t.Fatal(err)
		}
		wire := string(body)
		for _, forbidden := range []string{"\"wave\"", "miner", "colleague", "project-04", "trip-01", "shared-label", "shared-session", "project-subject", "trip-subject"} {
			if strings.Contains(wire, forbidden) {
				t.Errorf("wave %d leaks %q: %s", i, forbidden, wire)
			}
		}
	}
}

func TestV9CompleteHarnessBodiesContainNoCanonicalIdentifierAtAnyDepth(t *testing.T) {
	p := mustProjection(t, 537)
	canonical := map[string]bool{}
	for _, x := range p.Manifest.Users {
		canonical[x.Internal] = true
	}
	for _, x := range p.Manifest.Cases {
		canonical[x.Internal] = true
	}
	for _, x := range p.Manifest.Pairs {
		canonical[x.Internal] = true
	}
	for _, x := range p.Manifest.Sessions {
		canonical[x.Internal] = true
	}
	for _, x := range p.Manifest.Subjects {
		canonical[x.Internal] = true
	}
	assertBlind := func(surface string, body []byte) {
		t.Helper()
		for internal := range canonical {
			if internal != "" && strings.Contains(string(body), internal) {
				t.Errorf("%s contains canonical identifier %q at some serialized depth: %s", surface, internal, body)
			}
		}
	}
	for i, wave := range p.Waves {
		body, err := json.Marshal(wave)
		if err != nil {
			t.Fatal(err)
		}
		assertBlind("seed", body)
		if strings.Contains(string(body), `"wave"`) {
			t.Errorf("seed %d contains wave", i)
		}
	}
	primaryUser := p.WireUserID(PrimaryUser)
	for _, c := range p.ToolCases {
		body, err := json.Marshal(protocol.RunRequest{CaseID: c.ID, UserInput: c.Prompt, Tools: []protocol.ToolDefinition{}, BenchVersion: protocol.BenchVersionV9, UserID: primaryUser})
		if err != nil {
			t.Fatal(err)
		}
		assertBlind("tool run", body)
		for _, spec := range c.ExpectedTools {
			args, err := json.Marshal(spec.RequiredArgs)
			if err != nil {
				t.Fatal(err)
			}
			assertBlind("nested tool arguments", args)
		}
	}
	for _, sc := range p.MemoryCases {
		body, err := json.Marshal(protocol.RunRequest{CaseID: sc.Case.ID, UserInput: sc.Case.Question, Tools: []protocol.ToolDefinition{}, BenchVersion: protocol.BenchVersionV9, UserID: sc.UserID})
		if err != nil {
			t.Fatal(err)
		}
		assertBlind("memory run", body)
	}
}

func TestV9GeneratedDatasetProjectionHasNoCanonicalIdentifierOnHarnessSurfaces(t *testing.T) {
	const seed = int64(143)
	prof, ok := ProfileForVersion("small", protocol.BenchVersionV9)
	if !ok {
		t.Fatal("v9 small profile unavailable")
	}
	rng, err := NewRNGForVersion(seed, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, protocol.BenchVersionV9)
	suite, err := GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	iso, err := GenerateIsolationForVersion(seed, prof.Mem, prof.Waves, prof.IsoCases, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	suite.Cases = append(suite.Cases, iso.Cases...)
	waves := MergeMemoryWaves(suite.Waves, iso.SecondaryWave)
	p, err := BuildHarnessProjection(seed, testBlindingKey(0xa9), protocol.BenchVersionV9, tools, suite.Cases, waves)
	if err != nil {
		t.Fatal(err)
	}
	canonical := map[string]bool{}
	for _, entry := range p.Manifest.Users {
		canonical[entry.Internal] = true
	}
	for _, entry := range p.Manifest.Cases {
		canonical[entry.Internal] = true
	}
	for _, entry := range p.Manifest.Pairs {
		canonical[entry.Internal] = true
	}
	for _, entry := range p.Manifest.Sessions {
		canonical[entry.Internal] = true
	}
	for _, entry := range p.Manifest.Subjects {
		canonical[entry.Internal] = true
	}
	assertBlind := func(surface string, body []byte) {
		t.Helper()
		for internal := range canonical {
			if internal != "" && strings.Contains(string(body), internal) {
				t.Errorf("generated %s leaked canonical identifier %q: %s", surface, internal, body)
			}
		}
	}
	for _, wave := range p.Waves {
		body, err := json.Marshal(wave)
		if err != nil {
			t.Fatal(err)
		}
		assertBlind("seed", body)
	}
	for _, c := range p.ToolCases {
		body, err := json.Marshal(protocol.RunRequest{CaseID: c.ID, UserInput: c.Prompt, BenchVersion: protocol.BenchVersionV9, UserID: p.WireUserID(PrimaryUser)})
		if err != nil {
			t.Fatal(err)
		}
		assertBlind("tool run", body)
		for _, spec := range c.ExpectedTools {
			args, err := json.Marshal(spec.RequiredArgs)
			if err != nil {
				t.Fatal(err)
			}
			assertBlind("tool args", args)
		}
	}
	for _, sc := range p.MemoryCases {
		body, err := json.Marshal(protocol.RunRequest{CaseID: sc.Case.ID, UserInput: sc.Case.Question, BenchVersion: protocol.BenchVersionV9, UserID: sc.UserID})
		if err != nil {
			t.Fatal(err)
		}
		assertBlind("memory run", body)
	}
}

func TestV9ProjectionAliasesSeedBoundRequiredArguments(t *testing.T) {
	p := mustProjection(t, 537)
	for _, c := range p.ToolCases {
		for _, spec := range c.ExpectedTools {
			for key, value := range spec.RequiredArgs {
				if (key == "pair_id" || key == "pairIds") && !uuidPattern.MatchString(value) {
					t.Errorf("case %q %s still exposes %q", c.ID, key, value)
				}
			}
		}
	}
}

func TestV9ReverseMapRestoresReportsAndNestedArguments(t *testing.T) {
	p := mustProjection(t, 537)
	caseWire := p.Manifest.Cases[0].Wire
	if got, err := p.InternalCaseID(caseWire); err != nil || got != p.Manifest.Cases[0].Internal {
		t.Fatalf("case reverse = %q err=%v", got, err)
	}
	userWire := p.Manifest.Users[0].Wire
	if got, err := p.InternalUserID(userWire); err != nil || got != p.Manifest.Users[0].Internal {
		t.Fatalf("user reverse = %q err=%v", got, err)
	}
	pair := p.Manifest.Pairs[0]
	subject := p.Manifest.Subjects[0]
	calls := []protocol.ObservedToolCall{{Name: "fetch_memories", Args: json.RawMessage(`{"pairIds":["` + pair.Wire + `"],"nested":{"subject_id":"` + subject.Wire + `"},"ordinary":"keep"}`)}}
	got, err := p.InternalizeCalls(calls)
	if err != nil {
		t.Fatal(err)
	}
	var args map[string]any
	if err := json.Unmarshal(got[0].Args, &args); err != nil {
		t.Fatal(err)
	}
	if args["pairIds"].([]any)[0] != pair.Internal {
		t.Fatalf("pair not restored: %s", got[0].Args)
	}
	if args["nested"].(map[string]any)["subject_id"] != subject.Internal {
		t.Fatalf("subject not restored: %s", got[0].Args)
	}
	if args["ordinary"] != "keep" {
		t.Fatalf("ordinary value changed: %s", got[0].Args)
	}
	if string(calls[0].Args) == string(got[0].Args) {
		t.Fatal("input call was not independently projected")
	}
}

func TestV9UnknownReverseValuesRemainUntouched(t *testing.T) {
	p := mustProjection(t, 537)
	if _, err := p.InternalCaseID("unknown"); err == nil {
		t.Fatal("unknown case capability did not fail closed")
	}
	if _, err := p.InternalUserID("unknown"); err == nil {
		t.Fatal("unknown user capability did not fail closed")
	}
	if _, err := p.InternalizeCalls([]protocol.ObservedToolCall{{Name: "x", Args: json.RawMessage(`{"pair_id":"unknown"}`)}}); err == nil {
		t.Fatal("unknown pair capability did not fail closed")
	}
	if _, err := p.InternalizeCalls([]protocol.ObservedToolCall{{Name: "x", Args: json.RawMessage(`{"pair_id":`)}}); err == nil {
		t.Fatal("invalid structured identity arguments did not fail closed")
	}
	raw := json.RawMessage(`{"value":"unknown"}`)
	got, err := p.InternalizeCalls([]protocol.ObservedToolCall{{Name: "x", Args: raw}})
	if err != nil {
		t.Fatal(err)
	}
	if string(got[0].Args) != string(raw) {
		t.Fatalf("unknown JSON changed: got %s want %s", got[0].Args, raw)
	}
}

func TestV9ProjectionDoesNotMutateCanonicalInputs(t *testing.T) {
	tools, memory, waves := projectionFixture()
	before, err := json.Marshal(struct {
		Tools  []protocol.ToolCase
		Memory []StagedCase
		Waves  []protocol.SeedRequest
	}{tools, memory, waves})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := BuildHarnessProjection(537, testBlindingKey(0x53), protocol.BenchVersionV9, tools, memory, waves); err != nil {
		t.Fatal(err)
	}
	after, err := json.Marshal(struct {
		Tools  []protocol.ToolCase
		Memory []StagedCase
		Waves  []protocol.SeedRequest
	}{tools, memory, waves})
	if err != nil {
		t.Fatal(err)
	}
	if string(before) != string(after) {
		t.Fatal("projection mutated canonical inputs")
	}
}

func TestV9ManifestPinsInternalExecutionOrder(t *testing.T) {
	p := mustProjection(t, 537)
	var toolOrder []string
	for _, c := range p.ToolCases {
		id, err := p.InternalCaseID(c.ID)
		if err != nil {
			t.Fatal(err)
		}
		toolOrder = append(toolOrder, id)
	}
	if !slices.Equal(toolOrder, p.Manifest.ToolCaseOrder) {
		t.Fatalf("tool order mismatch: got %v manifest %v", toolOrder, p.Manifest.ToolCaseOrder)
	}
	var memoryOrder []string
	for _, c := range p.MemoryCases {
		id, err := p.InternalCaseID(c.Case.ID)
		if err != nil {
			t.Fatal(err)
		}
		memoryOrder = append(memoryOrder, id)
	}
	if !slices.Equal(memoryOrder, p.Manifest.MemoryCaseOrder) {
		t.Fatalf("memory order mismatch: got %v manifest %v", memoryOrder, p.Manifest.MemoryCaseOrder)
	}
}

func TestV9ManifestPersistsReplayKeyMaterial(t *testing.T) {
	p := mustProjection(t, 537)
	if p.Manifest.BlindingKey != strings.Repeat("53", 32) {
		t.Fatalf("manifest blinding key = %q", p.Manifest.BlindingKey)
	}
}
