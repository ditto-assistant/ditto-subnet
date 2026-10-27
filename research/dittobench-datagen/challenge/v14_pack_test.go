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
	if got := SupportedGradingBenchVersions(); !reflect.DeepEqual(got, []int{13, 14}) {
		t.Fatalf("supported = %v", got)
	}
	if GradingBenchVersion != protocol.BenchVersionV13 {
		t.Fatal("the baseline policy must stay v13: every pre-v14 generator emits it")
	}
	for round, want := range map[int][]int{12: nil, 13: {13}, 14: {13, 14}, 15: {13, 14}} {
		if got := GradingBenchVersionsThrough(round); !reflect.DeepEqual(got, want) {
			t.Errorf("through(%d) = %v, want %v", round, got, want)
		}
	}
	if SupportsGradingBenchVersion(12) || SupportsGradingBenchVersion(15) || !SupportsGradingBenchVersion(14) {
		t.Fatal("support set is wrong")
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
		"unsupported":       {13, 15},
		"not ascending":     {13, 13},
	} {
		bad := req
		bad.GradingBenchVersions = list
		if bad.Validate() == nil {
			t.Errorf("%s: %v accepted", name, list)
		}
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
	p.GradingBenchVersion = 15
	if r := Validate(p); r.Valid || !hasRule(r, "", "grading_bench_version") {
		t.Fatalf("v15 pack accepted: %+v", r.Issues)
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
