package providercert

import "sort"

func Summarize(results []Result) []ProviderSummary {
	type aggregate struct {
		s         ProviderSummary
		latencies []int64
	}
	byProvider := map[string]*aggregate{}
	for _, r := range results {
		a := byProvider[r.ProviderSlug]
		if a == nil {
			a = &aggregate{s: ProviderSummary{Provider: r.Provider, ProviderSlug: r.ProviderSlug}}
			byProvider[r.ProviderSlug] = a
		}
		a.s.Runs++
		if r.Success {
			a.s.Successes++
		}
		if r.Scenario != "text" {
			a.s.ToolRuns++
			if r.Success {
				a.s.ToolSuccesses++
			}
		}
		a.s.TotalCostUSD += r.Usage.Cost
		a.s.OutputTokens += r.Usage.CompletionTokens
		if r.LatencyMS > 0 {
			a.latencies = append(a.latencies, r.LatencyMS)
		}
	}
	keys := make([]string, 0, len(byProvider))
	for key := range byProvider {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make([]ProviderSummary, 0, len(keys))
	for _, key := range keys {
		a := byProvider[key]
		a.s.SuccessRate = ratio(a.s.Successes, a.s.Runs)
		a.s.ToolSuccessRate = ratio(a.s.ToolSuccesses, a.s.ToolRuns)
		sort.Slice(a.latencies, func(i, j int) bool { return a.latencies[i] < a.latencies[j] })
		a.s.P50LatencyMS = percentile(a.latencies, 0.50)
		a.s.P95LatencyMS = percentile(a.latencies, 0.95)
		var totalLatencyMS int64
		for _, latency := range a.latencies {
			totalLatencyMS += latency
		}
		if totalLatencyMS > 0 {
			a.s.EffectiveOutputTokensPerSecond = float64(a.s.OutputTokens) / (float64(totalLatencyMS) / 1000)
		}
		out = append(out, a.s)
	}
	return out
}

func ratio(n, d int) float64 {
	if d == 0 {
		return 0
	}
	return float64(n) / float64(d)
}

func percentile(values []int64, p float64) int64 {
	if len(values) == 0 {
		return 0
	}
	i := int(float64(len(values)-1)*p + 0.5)
	if i >= len(values) {
		i = len(values) - 1
	}
	return values[i]
}
