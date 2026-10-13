package rerankpool

import (
	"fmt"
	"regexp"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func pair(id, prompt, response string) protocol.MemoryPair {
	return protocol.MemoryPair{PairID: id, SessionID: "s", Timestamp: "2026-01-01T00:00:00Z", Prompt: prompt, Response: response}
}

// fillerPairs returns n unrelated memories so a synthetic graph reaches the
// minimum pool size.
func fillerPairs(prefix string, n int) []protocol.MemoryPair {
	out := make([]protocol.MemoryPair, n)
	for i := range out {
		out[i] = pair(fmt.Sprintf("%s-%d", prefix, i), fmt.Sprintf("note number %d about gardening", i), "noted")
	}
	return out
}

func syntheticArtifact() gen.DatasetArtifact {
	primary := append(fillerPairs("f", 25), pair("p-evidence", "My locker code is 4417", "Got it, locker code 4417."))
	other := []protocol.MemoryPair{pair("p-other-user", "My locker code is 9999", "Saved.")}
	late := []protocol.MemoryPair{pair("p-late", "Actually my locker code changed to 5120", "Updated.")}
	return gen.DatasetArtifact{
		Seed:         7,
		BenchVersion: protocol.BenchVersionV13,
		MemoryWaves: []protocol.SeedRequest{
			{UserID: "miner", Wave: 0, Pairs: primary},
			{UserID: "miner", Wave: 1, Pairs: late},
			{UserID: "other", Wave: 0, Pairs: other},
		},
		MemoryCases: []gen.ArtifactCase{
			{MemoryCase: protocol.MemoryCase{ID: "m1", Question: "What is my locker code?"}, V10EvidencePairIDs: []string{"p-evidence"}},
			{MemoryCase: protocol.MemoryCase{ID: "m2", Question: "No evidence case"}},
			{MemoryCase: protocol.MemoryCase{ID: "m3", Question: "What is my locker code now?"}, RunAfterWave: 1, V10EvidencePairIDs: []string{"p-late"}},
			{MemoryCase: protocol.MemoryCase{ID: "m4", Question: "   "}, V10EvidencePairIDs: []string{"p-evidence"}},
		},
	}
}

func TestBuildSyntheticGoldAndScoping(t *testing.T) {
	set, err := Build(syntheticArtifact(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if set.Stats.MemoryCases != 4 || set.Stats.WithEvidence != 3 || set.Stats.EmptyQuery != 1 || set.Stats.Kept != 2 {
		t.Fatalf("unexpected stats %+v", set.Stats)
	}
	m1 := set.Pools[0]
	if len(m1.Candidates) != DefaultPoolSize {
		t.Fatalf("pool size %d", len(m1.Candidates))
	}
	// BM25 puts the only memory mentioning the locker code first.
	if m1.Candidates[0].Text != "User: My locker code is 4417\nDitto: Got it, locker code 4417." {
		t.Fatalf("evidence not ranked first: %q", m1.Candidates[0].Text)
	}
	if len(m1.Gold) != 1 || m1.Gold[0] != "c00" {
		t.Fatalf("gold = %v", m1.Gold)
	}
	for _, c := range m1.Candidates {
		if c.Text == "User: My locker code is 9999\nDitto: Saved." {
			t.Fatal("another user's memory leaked into the pool")
		}
		if c.Text == "User: Actually my locker code changed to 5120\nDitto: Updated." {
			t.Fatal("a wave-1 memory leaked into a wave-0 case")
		}
	}
	m3 := set.Pools[1]
	found := false
	for _, c := range m3.Candidates {
		if c.Text == "User: Actually my locker code changed to 5120\nDitto: Updated." {
			found = true
		}
	}
	if !found {
		t.Fatal("wave-1 case cannot see its wave-1 evidence")
	}
}

func TestTooSmallGraphIsDropped(t *testing.T) {
	a := gen.DatasetArtifact{
		MemoryWaves: []protocol.SeedRequest{{Pairs: []protocol.MemoryPair{pair("p1", "a", "b")}}},
		MemoryCases: []gen.ArtifactCase{{MemoryCase: protocol.MemoryCase{ID: "m", Question: "a?"}, V10EvidencePairIDs: []string{"p1"}}},
	}
	set, err := Build(a, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if set.Stats.TooFewCandidates != 1 || len(set.Pools) != 0 {
		t.Fatalf("stats %+v", set.Stats)
	}
}

func TestLexicallyWeakGoldIsKeptLowInBaseOrder(t *testing.T) {
	pairs := fillerPairs("f", 30)
	pairs = append(pairs, pair("p-hidden", "zzz", "qqq"))
	a := gen.DatasetArtifact{
		MemoryWaves: []protocol.SeedRequest{{Pairs: pairs}},
		MemoryCases: []gen.ArtifactCase{{MemoryCase: protocol.MemoryCase{ID: "m", Question: "gardening note"}, V10EvidencePairIDs: []string{"p-hidden"}}},
	}
	set, err := Build(a, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if len(set.Pools) != 1 {
		t.Fatalf("stats %+v", set.Stats)
	}
	p := set.Pools[0]
	if len(p.Candidates) != DefaultPoolSize || len(p.Gold) != 1 || p.Gold[0] != "c19" {
		t.Fatalf("gold should be last in base order: gold=%v n=%d", p.Gold, len(p.Candidates))
	}
}

func TestEvidenceMissingFromGraphIsDropped(t *testing.T) {
	a := gen.DatasetArtifact{
		MemoryWaves: []protocol.SeedRequest{{Pairs: fillerPairs("f", 30)}},
		MemoryCases: []gen.ArtifactCase{{MemoryCase: protocol.MemoryCase{ID: "m", Question: "q"}, V10EvidencePairIDs: []string{"f-1", "not-seeded"}}},
	}
	set, err := Build(a, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if set.Stats.MissingEvidence != 1 || len(set.Pools) != 0 {
		t.Fatalf("stats %+v", set.Stats)
	}
}

func TestEvidenceFillingHalfThePoolIsDropped(t *testing.T) {
	ids := make([]string, 11)
	for i := range ids {
		ids[i] = fmt.Sprintf("f-%d", i)
	}
	a := gen.DatasetArtifact{
		MemoryWaves: []protocol.SeedRequest{{Pairs: fillerPairs("f", 30)}},
		MemoryCases: []gen.ArtifactCase{{MemoryCase: protocol.MemoryCase{ID: "m", Question: "q"}, V10EvidencePairIDs: ids}},
	}
	set, err := Build(a, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if set.Stats.TooMuchEvidence != 1 || len(set.Pools) != 0 {
		t.Fatalf("stats %+v", set.Stats)
	}
}

func TestCandidateTextIsBounded(t *testing.T) {
	long := make([]rune, MaxCandidateRunes+500)
	for i := range long {
		long[i] = 'é'
	}
	if got := truncateRunes(string(long), MaxCandidateRunes); len([]rune(got)) != MaxCandidateRunes {
		t.Fatalf("truncated to %d runes", len([]rune(got)))
	}
	if got := truncateRunes("short", MaxCandidateRunes); got != "short" {
		t.Fatalf("short text changed: %q", got)
	}
}

func TestRepeatedPairIDKeepsLatestContent(t *testing.T) {
	waves := []protocol.SeedRequest{
		{Wave: 0, Pairs: []protocol.MemoryPair{pair("p", "old", "old")}},
		{Wave: 1, Pairs: []protocol.MemoryPair{pair("p", "new", "new")}},
	}
	docs := graphPairs(waves, gen.PrimaryUser, 1)
	if len(docs) != 1 || docs[0].text != "User: new\nDitto: new" {
		t.Fatalf("docs = %+v", docs)
	}
}

func TestToolPrerequisitesAreThePrimaryUsersFirstWave(t *testing.T) {
	a := gen.DatasetArtifact{
		ToolCases: []protocol.ToolCase{{ID: "t1", PrerequisitePairs: []protocol.MemoryPair{pair("p-pre", "My vet is Dr. Okafor", "Noted.")}}},
		MemoryWaves: []protocol.SeedRequest{
			{UserID: "miner", Pairs: fillerPairs("f", 25)},
			{UserID: "colleague", Pairs: fillerPairs("g", 25)},
		},
		MemoryCases: []gen.ArtifactCase{
			{MemoryCase: protocol.MemoryCase{ID: "m", Question: "Who is my vet?"}, V10EvidencePairIDs: []string{"p-pre"}},
			{MemoryCase: protocol.MemoryCase{ID: "iso", Question: "Who is my vet?"}, UserID: "colleague", V10EvidencePairIDs: []string{"p-pre"}},
		},
	}
	set, err := Build(a, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if set.Stats.Kept != 1 || set.Stats.MissingEvidence != 1 {
		t.Fatalf("prerequisites must reach the primary user only: %+v", set.Stats)
	}
	if set.Pools[0].Gold[0] != "c00" {
		t.Fatalf("gold = %v", set.Pools[0].Gold)
	}
}

func TestBM25TiesKeepSeedingOrder(t *testing.T) {
	docs := []doc{{pairID: "a", text: "x", order: 0}, {pairID: "b", text: "x", order: 1}, {pairID: "c", text: "x", order: 2}}
	ranked := rankBM25("unrelated", docs)
	for i, d := range ranked {
		if d.order != i {
			t.Fatalf("tie order changed: %+v", ranked)
		}
	}
}

var aliasRE = regexp.MustCompile(`^c\d{2}$`)

// TestGeneratedPoolsInvariants runs the real generator and checks every pool.
func TestGeneratedPoolsInvariants(t *testing.T) {
	prof, ok := gen.ProfileForVersion("full", protocol.BenchVersionV13)
	if !ok {
		t.Fatal("no full v13 profile")
	}
	total := 0
	for _, seed := range []int64{1, 2, 3} {
		artifact, err := gen.GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		set, err := Build(artifact, Options{})
		if err != nil {
			t.Fatal(err)
		}
		s := set.Stats
		if s.WithEvidence != s.EmptyQuery+s.MissingEvidence+s.TooMuchEvidence+s.TooFewCandidates+s.Kept {
			t.Fatalf("seed %d: stats do not add up: %+v", seed, s)
		}
		// The scorer refuses a dataset whose evidence is not seeded
		// (validateV8EvidenceAvailability), so a miss here is a pool bug.
		if s.MissingEvidence != 0 {
			t.Fatalf("seed %d: evidence missing from seeded graph: %+v", seed, s)
		}
		for _, p := range set.Pools {
			if len(p.Candidates) != DefaultPoolSize {
				t.Fatalf("seed %d pool %s: %d candidates", seed, p.PoolID, len(p.Candidates))
			}
			ids := map[string]bool{}
			for _, c := range p.Candidates {
				if !aliasRE.MatchString(c.ID) || ids[c.ID] || c.Text == "" {
					t.Fatalf("seed %d pool %s: bad candidate %+v", seed, p.PoolID, c)
				}
				ids[c.ID] = true
			}
			if len(p.Gold) == 0 {
				t.Fatalf("seed %d pool %s: kept without gold", seed, p.PoolID)
			}
			for _, g := range p.Gold {
				if !ids[g] {
					t.Fatalf("seed %d pool %s: gold %s not a candidate", seed, p.PoolID, g)
				}
			}
		}
		total += len(set.Pools)
		t.Logf("seed %d: %+v", seed, s)
	}
	if total == 0 {
		t.Fatal("generator produced no pools")
	}
}

func TestBuildIsDeterministic(t *testing.T) {
	prof, _ := gen.ProfileForVersion("full", protocol.BenchVersionV13)
	digest := func() string {
		a, err := gen.GenerateDataset(42, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		set, err := Build(a, Options{})
		if err != nil {
			t.Fatal(err)
		}
		d, err := Digest(set)
		if err != nil {
			t.Fatal(err)
		}
		return d
	}
	if a, b := digest(), digest(); a != b {
		t.Fatalf("non-deterministic pools: %s vs %s", a, b)
	}
}

func TestSurfaceSaltChangesPools(t *testing.T) {
	prof, _ := gen.ProfileForVersion("full", protocol.BenchVersionV13)
	build := func(salt uint64) string {
		a, err := gen.GenerateDatasetWithSurface(42, prof, protocol.BenchVersionV13, gen.SurfaceOptions{Salt: salt})
		if err != nil {
			t.Fatal(err)
		}
		set, err := Build(a, Options{})
		if err != nil {
			t.Fatal(err)
		}
		d, _ := Digest(set)
		return d
	}
	if build(0) == build(7) {
		t.Fatal("surface salt did not change the pools")
	}
}
