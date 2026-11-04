package challenge

import (
	"bytes"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func v14ReferencePack(seed int64) Pack {
	p := ReferencePack(seed, testHotkey)
	p.GradingBenchVersion = GradingBenchVersionV14
	return p
}

func packBytes(t *testing.T, p Pack) []byte {
	t.Helper()
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestSupportedGradingBenchVersions(t *testing.T) {
	if got := SupportedGradingBenchVersions(); !reflect.DeepEqual(got, []int{13, 14, 15, 16}) {
		t.Fatalf("supported = %v", got)
	}
	if GradingBenchVersion != protocol.BenchVersionV13 {
		t.Fatal("the baseline policy must stay v13: every pre-v14 generator emits it")
	}
	// 16 and above take the three-policy window (GradingPolicyWindowV16): a
	// v16 round accepts {14, 15, 16}; a v17 round, with no policy 17 shipped,
	// accepts what is left of (14, 17], {15, 16}.
	for round, want := range map[int][]int{12: nil, 13: {13}, 14: {13, 14}, 15: {14, 15}, 16: {14, 15, 16}, 17: {15, 16}, 18: {16}, 19: nil} {
		if got := GradingBenchVersionsThrough(round); !reflect.DeepEqual(got, want) {
			t.Errorf("through(%d) = %v, want %v", round, got, want)
		}
	}
	if SupportsGradingBenchVersion(12) || SupportsGradingBenchVersion(17) || !SupportsGradingBenchVersion(14) || !SupportsGradingBenchVersion(15) || !SupportsGradingBenchVersion(16) {
		t.Fatal("support set is wrong")
	}
}

// TestGradingPolicyWindowIsTwoVersions pins the operator's support window
// below v16: a round at N accepts packs at N and N-1 only, so a third policy
// retires the oldest for rounds above it while an older round keeps it.
func TestGradingPolicyWindowIsTwoVersions(t *testing.T) {
	if GradingPolicyWindow != 2 {
		t.Fatalf("GradingPolicyWindow = %d; the operator fixed the window at two versions", GradingPolicyWindow)
	}
	for _, round := range []int{12, 13, 14, 15} {
		if got := GradingPolicyWindowFor(round); got != GradingPolicyWindow {
			t.Fatalf("GradingPolicyWindowFor(%d) = %d; rounds below 16 keep the two-version window", round, got)
		}
	}
	saved := supportedGradingBenchVersions
	t.Cleanup(func() { supportedGradingBenchVersions = saved })
	supportedGradingBenchVersions = []int{13, 14, 15}
	for round, want := range map[int][]int{13: {13}, 14: {13, 14}, 15: {14, 15}} {
		if got := GradingBenchVersionsThrough(round); !reflect.DeepEqual(got, want) {
			t.Errorf("with a third policy, through(%d) = %v, want %v", round, got, want)
		}
	}
	v15 := NewGenerateRequestFor(SeedFromInt64(7), testHotkey, 16, 15)
	if err := v15.Validate(); err != nil {
		t.Fatal(err)
	}
	if v15.GradingBenchVersion != 14 || !reflect.DeepEqual(v15.Accepted(), []int{14, 15}) {
		t.Fatalf("v15 request = %+v, want floor 14 accepting [14 15]", v15)
	}
	if v15.Accepts(13) || v15.AcceptsBaseline() {
		t.Fatal("a v15 round must not accept a v13 pack (no legacy retry either)")
	}
	if v14 := NewGenerateRequestFor(SeedFromInt64(7), testHotkey, 16, 14); !v14.AcceptsBaseline() || v14.GradingBenchVersion != 13 {
		t.Fatalf("a v14 round keeps the v13 baseline and its retry: %+v", v14)
	}
	if err := CheckRoundGradingVersions([]RoundColumnPack{{ColumnID: "c0", Pack: ReferencePack(3, testHotkey)}}, 15); err == nil {
		t.Fatal("a v15 round dataset must refuse a v13 column")
	}
}

// TestGradingPolicyWindowV16AcceptsPolicy14 pins the operator's v16 exception
// (2026-11-04): a v16 round or dueling season accepts {14, 15, 16}, so a
// generator that declares 14 keeps its v16 columns and battery seeds. The
// window slides from 16 on (three policies), so a v17 round would accept
// {15, 16, 17}.
func TestGradingPolicyWindowV16AcceptsPolicy14(t *testing.T) {
	if GradingPolicyWindowV16 != 3 || GradingPolicyWindowV16From != 16 {
		t.Fatalf("v16 window = %d from v%d; the operator fixed {14, 15, 16} at v16", GradingPolicyWindowV16, GradingPolicyWindowV16From)
	}
	for _, round := range []int{16, 17, 20} {
		if got := GradingPolicyWindowFor(round); got != GradingPolicyWindowV16 {
			t.Fatalf("GradingPolicyWindowFor(%d) = %d, want %d", round, got, GradingPolicyWindowV16)
		}
	}
	saved := supportedGradingBenchVersions
	t.Cleanup(func() { supportedGradingBenchVersions = saved })
	supportedGradingBenchVersions = []int{13, 14, 15, 16, 17}
	for round, want := range map[int][]int{15: {14, 15}, 16: {14, 15, 16}, 17: {15, 16, 17}} {
		if got := GradingBenchVersionsThrough(round); !reflect.DeepEqual(got, want) {
			t.Errorf("with a policy 17, through(%d) = %v, want %v", round, got, want)
		}
	}
	supportedGradingBenchVersions = saved

	v16 := NewGenerateRequestFor(SeedFromInt64(7), testHotkey, 16, 16)
	if err := v16.Validate(); err != nil {
		t.Fatal(err)
	}
	if v16.GradingBenchVersion != 14 || !reflect.DeepEqual(v16.Accepted(), []int{14, 15, 16}) {
		t.Fatalf("v16 request = %+v, want floor 14 accepting [14 15 16]", v16)
	}
	if v16.Accepts(13) || v16.AcceptsBaseline() {
		t.Fatal("a v16 round must not accept a v13 pack (no legacy retry either)")
	}
	// A generator built for {13, 14} picks 14 and still validates.
	if got := v16.NewestAccepted([]int{13, 14}); got != 14 {
		t.Fatalf("a {13, 14} generator picks v%d in a v16 round, want 14", got)
	}
	p14 := v14ReferencePack(3)
	if err := CheckRoundGradingVersions([]RoundColumnPack{{ColumnID: "c0", Pack: p14}}, 16); err != nil {
		t.Fatalf("a v16 round dataset must accept a v14 column: %v", err)
	}
	if err := CheckRoundGradingVersions([]RoundColumnPack{{ColumnID: "c0", Pack: ReferencePack(3, testHotkey)}}, 16); err == nil {
		t.Fatal("a v16 round dataset must refuse a v13 column")
	}
}

func TestV13RequestIsByteIdentical(t *testing.T) {
	seed := SeedFromInt64(7)
	legacy := NewGenerateRequest(seed, testHotkey, 16)
	for _, round := range []int{0, 12, 13} {
		got := NewGenerateRequestFor(seed, testHotkey, 16, round)
		if !bytes.Equal(got.Bytes(), legacy.Bytes()) {
			t.Fatalf("round %d request = %s, want the original %s", round, got.Bytes(), legacy.Bytes())
		}
	}
	if strings.Contains(string(legacy.Bytes()), "grading_bench_versions") {
		t.Fatal("a v13 request must not carry grading_bench_versions")
	}
}

func TestV14RequestAcceptsBothPolicies(t *testing.T) {
	req := NewGenerateRequestFor(SeedFromInt64(7), testHotkey, 16, 14)
	if err := req.Validate(); err != nil {
		t.Fatal(err)
	}
	if req.GradingBenchVersion != 13 || !reflect.DeepEqual(req.GradingBenchVersions, []int{13, 14}) {
		t.Fatalf("request = %+v", req)
	}
	if !req.Accepts(13) || !req.Accepts(14) || req.Accepts(15) {
		t.Fatal("accepts is wrong")
	}
	if got := req.NewestAccepted(nil); got != 14 {
		t.Fatalf("newest accepted = %d", got)
	}
	if got := req.NewestAccepted([]int{13}); got != 13 {
		t.Fatalf("a v13-only generator must pick 13, got %d", got)
	}
	if got := NewGenerateRequest(req.Seed, testHotkey, 16).NewestAccepted(nil); got != 13 {
		t.Fatalf("a v13 request must yield 13, got %d", got)
	}
	// A generator decoding into the original request shape (no
	// grading_bench_versions field) still reads a valid v13 request.
	var old struct {
		Contract            string `json:"contract"`
		Seed                string `json:"seed"`
		MinerHotkey         string `json:"miner_hotkey"`
		Cases               int    `json:"cases"`
		GradingBenchVersion int    `json:"grading_bench_version"`
	}
	if err := json.Unmarshal(req.Bytes(), &old); err != nil || old.GradingBenchVersion != 13 {
		t.Fatalf("legacy decode: %v %+v", err, old)
	}
	for name, list := range map[string][]int{
		"not from baseline": {14},
		"not ascending":     {13, 13},
	} {
		bad := req
		bad.GradingBenchVersions = list
		if bad.Validate() == nil {
			t.Errorf("%s: %v accepted", name, list)
		}
	}
	// A policy this package does not know yet is ignored, not refused, so a
	// generator built now keeps working when a newer scorer offers it.
	future := req
	future.GradingBenchVersions = []int{13, 14, 17}
	if err := future.Validate(); err != nil || future.NewestAccepted(nil) != 14 {
		t.Fatalf("a request offering an unknown v17 must validate and pick 14: %v", err)
	}
	// A request whose window moved past every policy this package knows is
	// refused: the generator must be rebuilt.
	moved := NewGenerateRequest(req.Seed, testHotkey, 16)
	moved.GradingBenchVersion, moved.GradingBenchVersions = 17, []int{17, 18}
	if moved.Validate() == nil {
		t.Fatal("a request accepting only unknown policies must be refused")
	}
	below := req
	below.GradingBenchVersion, below.GradingBenchVersions = 12, nil
	if below.Validate() == nil {
		t.Fatal("a request floor below the baseline must be refused")
	}
}

func TestV14PackValidatesAndBinds(t *testing.T) {
	p := v14ReferencePack(3)
	raw := packBytes(t, p)
	if r := Validate(p); !r.Valid {
		t.Fatalf("v14 reference pack invalid: %+v", r.Issues)
	}
	v14 := NewGenerateRequestFor(p.Generator.Seed, testHotkey, len(p.Cases), 14)
	if r := ValidateGenerated(raw, v14); !r.Valid {
		t.Fatalf("v14 pack for a v14 request: %+v", r.Issues)
	}
	v13 := NewGenerateRequest(p.Generator.Seed, testHotkey, len(p.Cases))
	if r := ValidateGenerated(raw, v13); r.Valid || !hasRule(r, "", "binding.grading_bench_version") {
		t.Fatalf("a v13 request must refuse a v14 pack: %+v", r.Issues)
	}
	// A v13 pack satisfies a v14 request: a generator that never learned v14
	// keeps its column.
	legacy := ReferencePack(3, testHotkey)
	if r := ValidateGenerated(packBytes(t, legacy), v14); !r.Valid {
		t.Fatalf("v13 pack for a v14 request: %+v", r.Issues)
	}
}

func TestPackGradingVersionReachesTheGrader(t *testing.T) {
	p := v14ReferencePack(3)
	for i := range p.Cases {
		if got := p.GradingCase(i).BenchVersion; got != 14 {
			t.Fatalf("case %d grades at %d", i, got)
		}
		if got := p.Cases[i].GradingCase().BenchVersion; got != 13 {
			t.Fatalf("baseline GradingCase must stay v13, got %d", got)
		}
	}
	legacy := ReferencePack(3, testHotkey)
	if got := legacy.GradingCase(0).BenchVersion; got != 13 {
		t.Fatalf("v13 pack grades at %d", got)
	}
}

func TestPackGradingVersionRules(t *testing.T) {
	p := v14ReferencePack(3)
	p.GradingBenchVersion = 17
	if r := Validate(p); r.Valid || !hasRule(r, "", "grading_bench_version") {
		t.Fatalf("v17 pack accepted: %+v", r.Issues)
	}
	p = v14ReferencePack(3)
	p.Cases[0].BenchVersion = 13
	if r := Validate(p); r.Valid || !hasRule(r, "", "bench_version") {
		t.Fatalf("a case may not pick another policy than its pack: %+v", r.Issues)
	}
	p.Cases[0].BenchVersion = 14
	if r := Validate(p); !r.Valid {
		t.Fatalf("a case may restate its pack's policy: %+v", r.Issues)
	}
	legacy := ReferencePack(3, testHotkey)
	legacy.Cases[0].BenchVersion = 14
	if r := Validate(legacy); r.Valid || !hasRule(r, "", "bench_version") {
		t.Fatalf("a v13 pack case may not select v14: %+v", r.Issues)
	}
}

func TestV14RenderedTextRule(t *testing.T) {
	mutate := func(p *Pack) {
		c := &p.Cases[0]
		c.DistractorAnswers = append([]string(nil), c.DistractorAnswers...)
		c.DistractorAnswers[0] = "**" + c.DistractorAnswers[0] + "**"
	}
	p := v14ReferencePack(3)
	mutate(&p)
	r := Validate(p)
	if r.Valid || !hasRule(r, "", "v14_rendered_text") {
		t.Fatalf("a markdown distractor must be refused at v14: %+v", r.Issues)
	}
	legacy := ReferencePack(3, testHotkey)
	mutate(&legacy)
	if hasRule(Validate(legacy), "", "v14_rendered_text") {
		t.Fatal("the v14 rule must not apply to a v13 pack")
	}
	p = v14ReferencePack(3)
	p.Cases[0].DumpGuard = []string{"`" + p.Cases[0].ExpectedAnswer + "`"}
	if r := Validate(p); !hasRule(r, "", "v14_rendered_text") {
		t.Fatalf("a markdown dump guard must be refused at v14: %+v", r.Issues)
	}
}

func TestCheckRoundGradingVersions(t *testing.T) {
	cols := []RoundColumnPack{
		{ColumnID: "a", Pack: ReferencePack(1, testHotkey)},
		{ColumnID: "b", Pack: v14ReferencePack(2)},
	}
	if err := CheckRoundGradingVersions(cols, 14); err != nil {
		t.Fatal(err)
	}
	if err := CheckRoundGradingVersions(cols[:1], 13); err != nil {
		t.Fatal(err)
	}
	if err := CheckRoundGradingVersions(cols, 13); err == nil || !strings.Contains(err.Error(), "column b") {
		t.Fatalf("a v13 round must refuse a v14 column: %v", err)
	}
}
