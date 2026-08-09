package datagen

import (
	"math"
	"math/rand"
	"reflect"
	"sort"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV9SamplerRejectsEmptyBudgets(t *testing.T) {
	r := rand.New(rand.NewSource(1))
	for _, tc := range []struct {
		n       int
		weights []int
	}{{0, []int{1}}, {-1, []int{1}}, {3, nil}} {
		if got := sampledCategoryOrderV9(r, tc.n, tc.weights, 0); len(got) != 0 {
			t.Fatalf("n=%d weights=%v returned %v", tc.n, tc.weights, got)
		}
	}
}

func TestV9SamplerIsDeterministicAndSeedVaried(t *testing.T) {
	weights := []int{1, 2, 4, 8, 1, 3}
	order := func(seed int64) []int {
		return sampledCategoryOrderV9(rand.New(rand.NewSource(seed)), 30, weights, 4)
	}
	if !reflect.DeepEqual(order(77), order(77)) {
		t.Fatal("same family-mix seed produced different orders")
	}
	if reflect.DeepEqual(histogram(order(77)), histogram(order(78))) {
		t.Fatal("adjacent family-mix seeds produced the same residual histogram")
	}
}

func TestV9SamplerFullCatalogFloorAndMandatoryCase(t *testing.T) {
	weights := []int{0, -5, 1, 2, 8, 3}
	got := sampledCategoryOrderV9(rand.New(rand.NewSource(9)), 24, weights, 1)
	if len(got) != 24 {
		t.Fatalf("len=%d, want 24", len(got))
	}
	counts := histogram(got)
	for i := range weights {
		if counts[i] < 1 {
			t.Errorf("family %d has no floor: %v", i, counts)
		}
	}
	if counts[1] < 1 {
		t.Fatal("mandatory family disappeared")
	}
}

func TestV9SamplerPartialCatalogIsWeightedWithoutReplacement(t *testing.T) {
	weights := []int{1, 1, 20, 1}
	hits := make([]int, len(weights))
	for seed := int64(1); seed <= 2_000; seed++ {
		order := sampledCategoryOrderV9(rand.New(rand.NewSource(seed)), 2, weights, 0)
		counts := histogram(order)
		if len(order) != 2 || counts[0] != 1 {
			t.Fatalf("seed %d order=%v must contain mandatory family 0", seed, order)
		}
		for family, count := range counts {
			if count != 1 {
				t.Fatalf("seed %d family %d repeated in without-replacement floor: %v", seed, family, order)
			}
			hits[family]++
		}
	}
	if hits[2] < 1_700 || hits[2] <= 10*hits[1] || hits[2] <= 10*hits[3] {
		t.Fatalf("weighted inclusion did not favor weight-20 family: %v", hits)
	}
}

func TestV9SamplerResidualMatchesPublishedWeights(t *testing.T) {
	weights := []int{1, 3, 9}
	const (
		seeds = 5_000
		n     = 29
	)
	totals := make([]int, len(weights))
	for seed := int64(1); seed <= seeds; seed++ {
		counts := histogram(sampledCategoryOrderV9(rand.New(rand.NewSource(seed)), n, weights, -1))
		for family := range weights {
			// Remove the one-per-family floor. Only the residual must follow the
			// weighted multinomial expectation.
			totals[family] += counts[family] - 1
		}
	}
	residualPerSeed := n - len(weights)
	totalWeight := 0
	for _, weight := range weights {
		totalWeight += weight
	}
	for family, weight := range weights {
		expected := float64(seeds*residualPerSeed*weight) / float64(totalWeight)
		// Six binomial standard deviations plus one count is deterministic and
		// tight enough to catch an unweighted or index-biased allocator.
		p := float64(weight) / float64(totalWeight)
		sigma := math.Sqrt(float64(seeds*residualPerSeed) * p * (1 - p))
		if delta := math.Abs(float64(totals[family]) - expected); delta > 6*sigma+1 {
			t.Errorf("family %d total=%d expected=%.1f delta=%.1f limit=%.1f", family, totals[family], expected, delta, 6*sigma+1)
		}
	}
}

func TestV9CategoryMixStreamIsDomainSeparated(t *testing.T) {
	a := toolCategoryMixRNG(42, protocol.BenchVersionV9, 100)
	b := toolCategoryMixRNG(42, protocol.BenchVersionV9, 100)
	c := toolCategoryMixRNG(42, protocol.BenchVersionV9, 48)
	for i := 0; i < 20; i++ {
		if a.Int63() != b.Int63() {
			t.Fatal("same seed/version/profile did not reproduce family-mix stream")
		}
	}
	if toolCategoryMixRNG(42, protocol.BenchVersionV8, 100).Int63() == toolCategoryMixRNG(42, protocol.BenchVersionV9, 100).Int63() {
		t.Fatal("bench-version bump did not rotate family-mix stream")
	}
	if toolCategoryMixRNG(42, protocol.BenchVersionV9, 100).Int63() == c.Int63() {
		t.Fatal("profile-size change did not rotate family-mix stream")
	}
}

func TestV9FinalSemanticFamilyFloorAcrossFortySeeds(t *testing.T) {
	expected := expectedV9SemanticFamilies()
	if len(expected) != 53 {
		t.Fatalf("expected final semantic inventory has %d families, want 53: %v", len(expected), sortedKeys(expected))
	}
	seenHistograms := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		renderRNG, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV9)
		if err != nil {
			t.Fatal(err)
		}
		cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(renderRNG)), seed, 100, protocol.BenchVersionV9)
		counts := map[string]int{}
		for _, tc := range cases {
			counts[tc.Category]++
		}
		if len(cases) != 100 {
			t.Fatalf("seed %d emitted %d cases, want 100", seed, len(cases))
		}
		if counts["set_effort"] < 1 {
			t.Errorf("seed %d omitted set_effort", seed)
		}
		worldCases, composedCases := v9DistributionCounts(counts)
		if worldCases < V9FullWorldActionMinimum || worldCases > V9FullWorldActionTarget {
			t.Errorf("seed %d world cases=%d, want [%d,%d]", seed, worldCases, V9FullWorldActionMinimum, V9FullWorldActionTarget)
		}
		if composedCases < V9FullComposedCaseMinimum {
			t.Errorf("seed %d composed cases=%d, want at least %d", seed, composedCases, V9FullComposedCaseMinimum)
		}
		for family := range expected {
			if counts[family] < 1 {
				t.Errorf("seed %d omitted final semantic family %q", seed, family)
			}
		}
		for family := range counts {
			if !expected[family] {
				t.Errorf("seed %d emitted unexpected/retired family %q", seed, family)
			}
		}
		seenHistograms[histogramKey(counts)] = true
	}
	if len(seenHistograms) < 35 {
		t.Fatalf("only %d distinct final histograms across 40 seeds", len(seenHistograms))
	}
}

func TestV9FinalDistributionContractAcrossThreeHundredSeeds(t *testing.T) {
	expected := expectedV9SemanticFamilies()
	countsByFamily := make(map[string][]int, len(expected))
	worldTotals := make([]int, 0, 300)
	composedTotals := make([]int, 0, 300)
	for seed := int64(1); seed <= 300; seed++ {
		rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV9)
		if err != nil {
			t.Fatal(err)
		}
		cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, V9FullToolCaseCount, protocol.BenchVersionV9)
		counts := map[string]int{}
		for _, tc := range cases {
			counts[tc.Category]++
		}
		for family := range expected {
			if counts[family] < 1 {
				t.Fatalf("seed %d omitted final family %q", seed, family)
			}
			countsByFamily[family] = append(countsByFamily[family], counts[family])
		}
		world, composed := v9DistributionCounts(counts)
		if world < V9FullWorldActionMinimum || world > V9FullWorldActionTarget {
			t.Fatalf("seed %d world cases=%d, want contract [%d,%d]", seed, world, V9FullWorldActionMinimum, V9FullWorldActionTarget)
		}
		if composed < V9FullComposedCaseMinimum {
			t.Fatalf("seed %d composed cases=%d, want at least %d", seed, composed, V9FullComposedCaseMinimum)
		}
		worldTotals = append(worldTotals, world)
		composedTotals = append(composedTotals, composed)
	}

	worldStats := distributionStats(worldTotals)
	composedStats := distributionStats(composedTotals)
	if worldStats.Min < V9FullWorldActionMinimum || worldStats.Max > V9FullWorldActionTarget || worldStats.Variance <= 0 {
		t.Errorf("world total stats=%+v, want range [%d,%d] with seed variation", worldStats, V9FullWorldActionMinimum, V9FullWorldActionTarget)
	}
	if composedStats.Mean < V9FullComposedCaseMinimum || composedStats.Variance <= 0 {
		t.Errorf("composed total lacks floor or seed variation: %+v", composedStats)
	}

	variableWorld, variableNonWorld := 0, 0
	for family, observations := range countsByFamily {
		stats := distributionStats(observations)
		if stats.Mean < 1 {
			t.Errorf("family %q mean=%f violates one-per-run floor", family, stats.Mean)
		}
		if stats.Variance > 0 {
			if v9WorldFamily(family) {
				variableWorld++
			} else {
				variableNonWorld++
			}
		}
	}
	if variableWorld != 7 {
		t.Errorf("only %d/7 world-family counts vary by seed", variableWorld)
	}
	if variableNonWorld < 12 {
		t.Errorf("only %d non-world family counts vary by seed; weighted residual collapsed", variableNonWorld)
	}

	stale := distributionStats(countsByFamily["stale_context_web"])
	memoryFetch := distributionStats(countsByFamily["memory_fetch"])
	if stale.Mean <= memoryFetch.Mean || stale.Variance <= 0 || memoryFetch.Variance <= 0 {
		t.Errorf("published source weights not visible in final residual: stale=%+v memory_fetch=%+v", stale, memoryFetch)
	}
	contact := distributionStats(countsByFamily["world_contact_research_email_result_usage"])
	for _, family := range []string{
		"world_business_workflow", "world_link_chain_result_usage", "world_memory_delete",
		"world_memory_update", "world_theme_discover_set",
	} {
		other := distributionStats(countsByFamily[family])
		if contact.Mean <= other.Mean {
			t.Errorf("two-slot contact world draw mean=%f not above %s mean=%f", contact.Mean, family, other.Mean)
		}
	}
	t.Logf("300-seed final distribution: world=%+v composed=%+v stale=%+v memory_fetch=%+v variable_world=%d variable_nonworld=%d", worldStats, composedStats, stale, memoryFetch, variableWorld, variableNonWorld)
}

func TestV9SetEffortIsMandatoryInEveryPublicProfile(t *testing.T) {
	for _, profile := range []struct {
		name string
		n    int
	}{{"small", 6}, {"medium", 48}, {"full", 100}} {
		for seed := int64(1); seed <= 20; seed++ {
			rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV9)
			if err != nil {
				t.Fatal(err)
			}
			cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, profile.n, protocol.BenchVersionV9)
			found := false
			for _, tc := range cases {
				found = found || tc.Category == "set_effort"
			}
			if !found {
				t.Errorf("%s seed %d omitted set_effort", profile.name, seed)
			}
		}
	}
}

func histogram(order []int) map[int]int {
	out := map[int]int{}
	for _, family := range order {
		out[family]++
	}
	return out
}

func expectedV9SemanticFamilies() map[string]bool {
	retired := map[string]bool{
		"email_send": true, "memory_delete": true, "memory_update": true, "link_read": true,
		"multi_job_status": true, "job_chain_result_usage": true, "job_chain_recovery_result_usage": true,
	}
	out := map[string]bool{}
	for _, c := range categoriesForVersion(protocol.BenchVersionV9) {
		if !retired[c.name] {
			out[c.name] = true
		}
	}
	for _, family := range []string{
		"world_agent_job_dispatch", "world_business_workflow", "world_contact_research_email_result_usage",
		"world_link_chain_result_usage", "world_memory_delete", "world_memory_update", "world_theme_discover_set",
	} {
		out[family] = true
	}
	return out
}

func histogramKey(counts map[string]int) string {
	keys := sortedKeys(counts)
	key := ""
	for _, family := range keys {
		key += family + "=" + string(rune(counts[family])) + ";"
	}
	return key
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

type observedDistribution struct {
	Mean     float64
	Variance float64
	Min      int
	Max      int
}

func distributionStats(values []int) observedDistribution {
	if len(values) == 0 {
		return observedDistribution{}
	}
	stats := observedDistribution{Min: values[0], Max: values[0]}
	total := 0
	for _, value := range values {
		total += value
		if value < stats.Min {
			stats.Min = value
		}
		if value > stats.Max {
			stats.Max = value
		}
	}
	stats.Mean = float64(total) / float64(len(values))
	for _, value := range values {
		delta := float64(value) - stats.Mean
		stats.Variance += delta * delta
	}
	stats.Variance /= float64(len(values))
	return stats
}

func v9DistributionCounts(counts map[string]int) (world, composed int) {
	for family, count := range counts {
		if v9WorldFamily(family) {
			world += count
		}
		if v9ComposedFamily(family) {
			composed += count
		}
	}
	return world, composed
}
