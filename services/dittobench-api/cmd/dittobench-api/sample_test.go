package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func getSample(t *testing.T, s *server, query string) (*httptest.ResponseRecorder, gen.DatasetArtifact) {
	t.Helper()
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/sample"+query, nil)
	s.handleSample(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d (body %s)", rr.Code, rr.Body.String())
	}
	var art gen.DatasetArtifact
	if err := json.Unmarshal(rr.Body.Bytes(), &art); err != nil {
		t.Fatalf("decode: %v (body %s)", err, rr.Body.String())
	}
	return rr, art
}

// TestSampleUsesPublicNegativeSeed pins the load-bearing anti-gaming guarantee:
// the sampler's seed is always in the reserved negative namespace, disjoint from
// every (non-negative) per-submission seed, so a sample can never be a scored
// dataset.
func TestSampleUsesPublicNegativeSeed(t *testing.T) {
	s := &server{}
	for i := 0; i <= maxSampleIndex; i++ {
		_, art := getSample(t, s, "?sample="+itoa(i))
		if art.Seed >= 0 {
			t.Fatalf("sample %d seed %d is not negative (would risk colliding with a real seed)", i, art.Seed)
		}
		if art.Seed != publicSampleSeed(i) {
			t.Fatalf("sample %d seed mismatch: got %d want %d", i, art.Seed, publicSampleSeed(i))
		}
	}
}

// TestSampleIncludesAnswerKeys asserts the sample is the FULL artifact, answer
// keys included. The sampler is un-redacted on purpose: with the generator public
// the full dataset is derivable anyway, so the transparency window shows the same
// bytes a validator scores.
func TestSampleIncludesAnswerKeys(t *testing.T) {
	s := &server{}
	rr, art := getSample(t, s, "?run_size=full")
	raw := rr.Body.String()
	// The grading fields that redaction used to strip must now be present.
	for _, want := range []string{
		"expected_tools", "expected_answer", "tool_fixtures",
	} {
		if !strings.Contains(raw, want) {
			t.Fatalf("sample should carry answer-key field %q but did not: %s", want, raw)
		}
	}
	if len(art.ToolCases) == 0 || len(art.MemoryCases) == 0 {
		t.Fatalf("expected tool + memory cases in the sample, got %d/%d", len(art.ToolCases), len(art.MemoryCases))
	}
	if len(art.ToolFixtures) == 0 {
		t.Fatal("expected tool fixtures (the served needle facts) in the full sample")
	}
}

// TestSampleShapeMatchesProfile checks the sample is a real full-profile dataset
// (60 tool cases), so the community sees the real size, and it pins the
// executable contracts at the API surface without changing the generator's
// separate CurrentBenchVersion activation boundary.
func TestSampleShapeMatchesProfile(t *testing.T) {
	s := &server{}

	for _, version := range []int{8, 9, 10} {
		_, artifact := getSample(t, s, "?run_size=full&bench_version="+itoa(version))
		if artifact.BenchVersion != version {
			t.Fatalf("sample version = %d, want %d", artifact.BenchVersion, version)
		}
		if len(artifact.ToolCases) == 0 || userScopedCases(artifact) == 0 {
			t.Fatalf("full v%d profile must include tool and user-scoped memory cases", version)
		}
		if len(artifact.MemoryWaves) < 1 {
			t.Fatalf("v%d expected at least one memory wave, got %d", version, len(artifact.MemoryWaves))
		}
	}
}

// TestSampleDeterministic: the same (run_size, sample) yields identical bytes.
// userScopedCases counts cases carrying an explicit memory graph.
func userScopedCases(art gen.DatasetArtifact) int {
	n := 0
	for _, c := range art.MemoryCases {
		if c.UserID != "" {
			n++
		}
	}
	return n
}

func TestSampleDeterministic(t *testing.T) {
	s := &server{}
	rr1, _ := getSample(t, s, "?run_size=medium&sample=3")
	rr2, _ := getSample(t, s, "?run_size=medium&sample=3")
	if rr1.Body.String() != rr2.Body.String() {
		t.Fatal("sampler is not deterministic for the same (run_size, sample)")
	}
	// A different sample index gives a different dataset.
	rr3, _ := getSample(t, s, "?run_size=medium&sample=4")
	if rr1.Body.String() == rr3.Body.String() {
		t.Fatal("different sample indices produced identical datasets")
	}
}

func TestSampleRejectsBadInput(t *testing.T) {
	s := &server{}
	cases := []struct{ name, query, wantSubstr string }{
		{"bad run_size", "?run_size=huge", "run_size must be one of"},
		{"index too high", "?sample=99", "sample must be between"},
		{"negative index", "?sample=-1", "sample must be between"},
		{"retired bench version", "?bench_version=7", "unsupported bench_version"},
		{"unsupported bench version", "?bench_version=11", "unsupported bench_version"},
		{"non-integer bench version", "?bench_version=latest", "bench_version must be an integer"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rr := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, "/v1/sample"+c.query, nil)
			s.handleSample(rr, req)
			if rr.Code != http.StatusBadRequest {
				t.Fatalf("expected 400, got %d (body %s)", rr.Code, rr.Body.String())
			}
			if !strings.Contains(rr.Body.String(), c.wantSubstr) {
				t.Fatalf("expected error to mention %q, got %s", c.wantSubstr, rr.Body.String())
			}
		})
	}
}

// TestSampleDefaultsToSmall: no run_size defaults to the small profile (6 tools).
func TestSampleDefaultsToSmall(t *testing.T) {
	s := &server{}
	_, art := getSample(t, s, "")
	if len(art.ToolCases) != 6 {
		t.Fatalf("small profile should have 6 tool cases, got %d", len(art.ToolCases))
	}
	if art.BenchVersion != 9 {
		t.Fatalf("omitted practice version must select v9, got %d", art.BenchVersion)
	}
}

func TestSampleCanExplicitlySelectV8(t *testing.T) {
	s := &server{}
	_, art := getSample(t, s, "?run_size=small&sample=2&bench_version=8")
	if art.BenchVersion != 8 {
		t.Fatalf("wrong version: %d", art.BenchVersion)
	}
}

func TestPracticeCatalogDefaultsToV9(t *testing.T) {
	s := &server{}
	rr := httptest.NewRecorder()
	s.handleCatalog(rr, httptest.NewRequest(http.MethodGet, "/v1/catalog", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("catalog status = %d, want 200: %s", rr.Code, rr.Body.String())
	}
	if got := rr.Header().Get("X-Bench-Version"); got != "9" {
		t.Fatalf("catalog version header = %q, want 9", got)
	}
	var got []protocol.ToolDefinition
	if err := json.Unmarshal(rr.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode catalog: %v", err)
	}
	want := catalog.CatalogForVersion(protocol.BenchVersionV9)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("practice catalog does not match v9: got %#v want %#v", got, want)
	}
}

func TestPracticeDatasetDefaultsToV9AndCanSelectV8(t *testing.T) {
	s := &server{allowPrivate: true}
	for _, tc := range []struct {
		name    string
		query   string
		version int
	}{
		{name: "default v9", query: "?seed=42&n=12", version: protocol.BenchVersionV9},
		{name: "explicit v8", query: "?seed=42&n=12&bench_version=8", version: protocol.BenchVersionV8},
		{name: "explicit v10", query: "?seed=42&n=12&bench_version=10", version: protocol.BenchVersionV10},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rr := httptest.NewRecorder()
			s.handleDataset(rr, httptest.NewRequest(http.MethodGet, "/v1/dataset"+tc.query, nil))
			if rr.Code != http.StatusOK {
				t.Fatalf("dataset status = %d, want 200: %s", rr.Code, rr.Body.String())
			}
			if got := rr.Header().Get("X-Bench-Version"); got != itoa(tc.version) {
				t.Fatalf("dataset version header = %q, want %d", got, tc.version)
			}
			var got protocol.Dataset
			if err := json.Unmarshal(rr.Body.Bytes(), &got); err != nil {
				t.Fatalf("decode dataset: %v", err)
			}
			want, err := datagen.GenerateForVersion(42, 12, tc.version)
			if err != nil {
				t.Fatal(err)
			}
			gotJSON, _ := json.Marshal(got)
			wantJSON, _ := json.Marshal(want)
			if !reflect.DeepEqual(gotJSON, wantJSON) {
				t.Fatalf("dataset does not match explicit v%d generation", tc.version)
			}
		})
	}
}

func TestPracticeDatasetAndCatalogRejectUnsupportedVersions(t *testing.T) {
	s := &server{allowPrivate: true}
	for _, path := range []string{
		"/v1/dataset?bench_version=7",
		"/v1/catalog?bench_version=11",
		"/v1/dataset?bench_version=not-a-number",
	} {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, path, nil)
		if strings.HasPrefix(path, "/v1/catalog") {
			s.handleCatalog(rr, req)
		} else {
			s.handleDataset(rr, req)
		}
		if rr.Code != http.StatusBadRequest {
			t.Fatalf("%s status = %d, want 400: %s", path, rr.Code, rr.Body.String())
		}
	}
}

// itoa is a tiny local int->string without importing strconv into the test twice.
func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [20]byte
	pos := len(b)
	for i > 0 {
		pos--
		b[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		b[pos] = '-'
	}
	return string(b[pos:])
}
