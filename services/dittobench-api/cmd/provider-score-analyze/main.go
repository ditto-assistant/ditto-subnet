// Command provider-score-analyze summarizes local top-agent benchmark runs
// against their already-recorded Chutes baselines. It consumes only sanitized
// score reports and relay telemetry; miner source and model credentials never
// enter the output.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"time"
)

var resultName = regexp.MustCompile(`^top3-([a-z0-9]+)-([0-9a-f-]{36})-run([0-9]+)\.json$`)

type baseline struct {
	BenchVersion int             `json:"bench_version"`
	RunSize      string          `json:"run_size"`
	Agents       []baselineAgent `json:"agents"`
}

type baselineAgent struct {
	Rank             int             `json:"rank"`
	AgentID          string          `json:"agent_id"`
	AgentName        string          `json:"agent_name"`
	AgentVersion     int             `json:"agent_version"`
	ChutesComposite  float64         `json:"chutes_composite"`
	ChutesToolMean   float64         `json:"chutes_tool_mean"`
	ChutesMemoryMean float64         `json:"chutes_memory_mean"`
	ChutesMedianMS   int64           `json:"chutes_median_ms"`
	DatasetSeed      int64           `json:"dataset_seed"`
	DatasetSHA256    string          `json:"dataset_sha256"`
	ArtifactSHA256   string          `json:"artifact_sha256"`
	ChutesCategories []categoryScore `json:"chutes_per_category"`
}

type runFile struct {
	Status    string       `json:"status"`
	Seed      int64        `json:"seed"`
	RunSize   string       `json:"run_size"`
	CreatedAt time.Time    `json:"created_at"`
	UpdatedAt time.Time    `json:"updated_at"`
	Report    *scoreReport `json:"report"`
}

type scoreReport struct {
	Composite  float64         `json:"composite"`
	ToolMean   float64         `json:"tool_mean"`
	MemoryMean float64         `json:"memory_mean"`
	MedianMS   int64           `json:"median_ms"`
	Categories []categoryScore `json:"per_category"`
	Details    scoreDetails    `json:"details"`
}

type scoreDetails struct {
	DatasetSHA256 string `json:"dataset_sha256"`
}

type categoryScore struct {
	Category string  `json:"category"`
	Count    int     `json:"count"`
	Mean     float64 `json:"mean"`
}

type relayFile struct {
	Model              string         `json:"model"`
	Provider           string         `json:"provider"`
	Requests           int            `json:"requests"`
	HTTPErrorResponses int            `json:"http_error_responses"`
	APIErrorResponses  int            `json:"api_error_responses"`
	UpstreamErrors     int            `json:"upstream_errors"`
	PromptTokens       int            `json:"prompt_tokens"`
	CompletionTokens   int            `json:"completion_tokens"`
	TotalTokens        int            `json:"total_tokens"`
	CostUSD            float64        `json:"cost_usd"`
	P50LatencyMS       int64          `json:"p50_latency_ms"`
	P95LatencyMS       int64          `json:"p95_latency_ms"`
	ResponseProviders  map[string]int `json:"response_providers"`
}

type observation struct {
	Provider string
	Agent    baselineAgent
	Run      int
	Score    scoreReport
	Relay    relayFile
	WallTime time.Duration
}

type analysis struct {
	SchemaVersion int               `json:"schema_version"`
	GeneratedAt   time.Time         `json:"generated_at"`
	BenchVersion  int               `json:"bench_version"`
	RunSize       string            `json:"run_size"`
	ExpectedRuns  int               `json:"expected_runs_per_provider_agent"`
	ValidRuns     int               `json:"valid_runs"`
	Providers     []providerSummary `json:"providers"`
}

type providerSummary struct {
	ClosenessRank               int               `json:"closeness_rank"`
	Provider                    string            `json:"provider"`
	Complete                    bool              `json:"complete"`
	ValidRuns                   int               `json:"valid_runs"`
	ExpectedRuns                int               `json:"expected_runs"`
	RunSuccessRate              float64           `json:"run_success_rate"`
	MeanAbsoluteCompositeDelta  float64           `json:"mean_absolute_composite_delta"`
	MeanSignedCompositeDelta    float64           `json:"mean_signed_composite_delta"`
	MeanCrossRunCompositeStdDev float64           `json:"mean_cross_run_composite_stddev"`
	TotalCostUSD                float64           `json:"total_cost_usd"`
	MeanCostPerRunUSD           float64           `json:"mean_cost_per_run_usd"`
	TotalRequests               int               `json:"total_requests"`
	RelayErrorRate              float64           `json:"relay_error_rate"`
	TotalPromptTokens           int               `json:"total_prompt_tokens"`
	TotalCompletionTokens       int               `json:"total_completion_tokens"`
	TotalBenchmarkWallSeconds   float64           `json:"total_benchmark_wall_seconds"`
	EffectiveCompletionTokPerS  float64           `json:"effective_completion_tokens_per_benchmark_second"`
	MedianRunP50RelayLatencyMS  int64             `json:"median_run_p50_relay_latency_ms"`
	MedianRunP95RelayLatencyMS  int64             `json:"median_run_p95_relay_latency_ms"`
	ResponseProviderAttribution map[string]int    `json:"response_provider_attribution"`
	Agents                      []agentSummary    `json:"agents"`
	PerCategoryChanges          []categorySummary `json:"per_category_changes"`
}

type agentSummary struct {
	Rank                      int               `json:"rank"`
	AgentID                   string            `json:"agent_id"`
	AgentName                 string            `json:"agent_name"`
	AgentVersion              int               `json:"agent_version"`
	ArtifactSHA256            string            `json:"artifact_sha256"`
	DatasetSeed               int64             `json:"dataset_seed"`
	DatasetSHA256             string            `json:"dataset_sha256"`
	Runs                      int               `json:"runs"`
	ChutesComposite           float64           `json:"chutes_composite"`
	OpenRouterCompositeMean   float64           `json:"openrouter_composite_mean"`
	CompositeDelta            float64           `json:"composite_delta"`
	AbsoluteCompositeDelta    float64           `json:"absolute_composite_delta"`
	OpenRouterCompositeStdDev float64           `json:"openrouter_composite_stddev"`
	OpenRouterCompositeMin    float64           `json:"openrouter_composite_min"`
	OpenRouterCompositeMax    float64           `json:"openrouter_composite_max"`
	ChutesToolMean            float64           `json:"chutes_tool_mean"`
	OpenRouterToolMean        float64           `json:"openrouter_tool_mean"`
	ToolMeanDelta             float64           `json:"tool_mean_delta"`
	ChutesMemoryMean          float64           `json:"chutes_memory_mean"`
	OpenRouterMemoryMean      float64           `json:"openrouter_memory_mean"`
	MemoryMeanDelta           float64           `json:"memory_mean_delta"`
	ChutesMedianMS            int64             `json:"chutes_median_ms"`
	OpenRouterMedianMSMean    float64           `json:"openrouter_median_ms_mean"`
	OpenRouterVsChutesLatency float64           `json:"openrouter_vs_chutes_latency_ratio"`
	TotalCostUSD              float64           `json:"total_cost_usd"`
	RunResults                []agentRunSummary `json:"run_results"`
}

type agentRunSummary struct {
	Run                         int            `json:"run"`
	Composite                   float64        `json:"composite"`
	ToolMean                    float64        `json:"tool_mean"`
	MemoryMean                  float64        `json:"memory_mean"`
	MedianMS                    int64          `json:"median_ms"`
	CostUSD                     float64        `json:"cost_usd"`
	Requests                    int            `json:"requests"`
	RelayErrors                 int            `json:"relay_errors"`
	P50RelayLatencyMS           int64          `json:"p50_relay_latency_ms"`
	P95RelayLatencyMS           int64          `json:"p95_relay_latency_ms"`
	BenchmarkWallSeconds        float64        `json:"benchmark_wall_seconds"`
	ResponseProviderAttribution map[string]int `json:"response_provider_attribution"`
}

type categorySummary struct {
	Category       string  `json:"category"`
	Count          int     `json:"count"`
	ChutesMean     float64 `json:"chutes_mean"`
	OpenRouterMean float64 `json:"openrouter_mean"`
	Delta          float64 `json:"delta"`
}

type categoryAccumulator struct {
	OpenRouterWeighted float64
	ChutesWeighted     float64
	Count              int
}

func main() {
	baselinePath := flag.String("baseline", "docs/provider-certification/top3-baseline-2026-07-17.json", "top-three Chutes baseline JSON")
	resultsDir := flag.String("results", "benchmark-results", "directory containing top3-*.json run and relay files")
	outputPath := flag.String("output", "", "write JSON here instead of stdout")
	expectedRuns := flag.Int("expected-runs", 3, "expected repetitions per provider and agent")
	flag.Parse()
	if *expectedRuns < 1 {
		fatal(errors.New("-expected-runs must be positive"))
	}

	base := readJSON[baseline](*baselinePath)
	observations, err := loadObservations(*resultsDir, base, *expectedRuns)
	if err != nil {
		fatal(err)
	}
	result := summarize(base, observations, *expectedRuns)
	encoded, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fatal(err)
	}
	encoded = append(encoded, '\n')
	if *outputPath == "" {
		_, _ = os.Stdout.Write(encoded)
		return
	}
	if err := os.WriteFile(*outputPath, encoded, 0o644); err != nil {
		fatal(err)
	}
}

func loadObservations(dir string, base baseline, expectedRuns int) ([]observation, error) {
	agents := make(map[string]baselineAgent, len(base.Agents))
	for _, agent := range base.Agents {
		agents[agent.AgentID] = agent
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var observations []observation
	seen := map[string]struct{}{}
	for _, entry := range entries {
		match := resultName.FindStringSubmatch(entry.Name())
		if entry.IsDir() || match == nil {
			continue
		}
		agent, ok := agents[match[2]]
		if !ok {
			return nil, fmt.Errorf("%s: agent is not in baseline", entry.Name())
		}
		var runNumber int
		if _, err := fmt.Sscanf(match[3], "%d", &runNumber); err != nil {
			return nil, fmt.Errorf("%s: parse run number: %w", entry.Name(), err)
		}
		if runNumber < 1 || runNumber > expectedRuns {
			return nil, fmt.Errorf("%s: run number must be in [1,%d]", entry.Name(), expectedRuns)
		}
		cell := match[1] + "/" + match[2] + fmt.Sprintf("/%d", runNumber)
		if _, exists := seen[cell]; exists {
			return nil, fmt.Errorf("%s: duplicate provider/agent/run cell", entry.Name())
		}
		seen[cell] = struct{}{}
		run := readJSON[runFile](filepath.Join(dir, entry.Name()))
		if run.Status != "done" || run.Report == nil {
			continue
		}
		if run.Seed != agent.DatasetSeed {
			return nil, fmt.Errorf("%s: seed %d does not match baseline %d", entry.Name(), run.Seed, agent.DatasetSeed)
		}
		if run.RunSize != base.RunSize {
			return nil, fmt.Errorf("%s: run size %q does not match baseline %q", entry.Name(), run.RunSize, base.RunSize)
		}
		if run.Report.Details.DatasetSHA256 != agent.DatasetSHA256 {
			return nil, fmt.Errorf("%s: dataset SHA-256 %q does not match baseline %q", entry.Name(), run.Report.Details.DatasetSHA256, agent.DatasetSHA256)
		}
		relayPath := filepath.Join(dir, entry.Name()[:len(entry.Name())-len(".json")]+"-relay.json")
		relay := readJSON[relayFile](relayPath)
		wallTime := run.UpdatedAt.Sub(run.CreatedAt)
		if wallTime < 0 {
			return nil, fmt.Errorf("%s: updated_at precedes created_at", entry.Name())
		}
		observations = append(observations, observation{Provider: match[1], Agent: agent, Run: runNumber, Score: *run.Report, Relay: relay, WallTime: wallTime})
	}
	return observations, nil
}

func summarize(base baseline, observations []observation, expectedRuns int) analysis {
	byProvider := map[string][]observation{}
	for _, item := range observations {
		byProvider[item.Provider] = append(byProvider[item.Provider], item)
	}
	result := analysis{SchemaVersion: 1, GeneratedAt: time.Now().UTC(), BenchVersion: base.BenchVersion, RunSize: base.RunSize, ExpectedRuns: expectedRuns, ValidRuns: len(observations)}
	for provider, items := range byProvider {
		result.Providers = append(result.Providers, summarizeProvider(provider, items, len(base.Agents), expectedRuns))
	}
	sort.Slice(result.Providers, func(i, j int) bool {
		left, right := result.Providers[i], result.Providers[j]
		if left.Complete != right.Complete {
			return left.Complete
		}
		if left.MeanAbsoluteCompositeDelta == right.MeanAbsoluteCompositeDelta {
			return left.MeanCrossRunCompositeStdDev < right.MeanCrossRunCompositeStdDev
		}
		return left.MeanAbsoluteCompositeDelta < right.MeanAbsoluteCompositeDelta
	})
	rank := 0
	for i := range result.Providers {
		if result.Providers[i].Complete {
			rank++
			result.Providers[i].ClosenessRank = rank
		}
	}
	return result
}

func summarizeProvider(provider string, items []observation, agentCount, expectedRuns int) providerSummary {
	expected := agentCount * expectedRuns
	byAgent := map[string][]observation{}
	var totalCost, wallSeconds float64
	var totalRequests, relayErrors, promptTokens, completionTokens int
	var p50s, p95s []int64
	attribution := map[string]int{}
	categories := map[string]categoryAccumulator{}
	for _, item := range items {
		byAgent[item.Agent.AgentID] = append(byAgent[item.Agent.AgentID], item)
		totalCost += item.Relay.CostUSD
		totalRequests += item.Relay.Requests
		relayErrors += max(item.Relay.HTTPErrorResponses, item.Relay.APIErrorResponses) + item.Relay.UpstreamErrors
		promptTokens += item.Relay.PromptTokens
		completionTokens += item.Relay.CompletionTokens
		wallSeconds += item.WallTime.Seconds()
		p50s = append(p50s, item.Relay.P50LatencyMS)
		p95s = append(p95s, item.Relay.P95LatencyMS)
		for name, count := range item.Relay.ResponseProviders {
			attribution[name] += count
		}
		for _, category := range item.Score.Categories {
			acc := categories[category.Category]
			acc.OpenRouterWeighted += category.Mean * float64(category.Count)
			acc.ChutesWeighted += baselineCategoryMean(item.Agent, category.Category) * float64(category.Count)
			acc.Count += category.Count
			categories[category.Category] = acc
		}
	}
	complete := len(items) == expected && len(byAgent) == agentCount
	if complete {
		for _, agentItems := range byAgent {
			if len(agentItems) != expectedRuns {
				complete = false
				break
			}
		}
	}
	summary := providerSummary{Provider: provider, Complete: complete, ValidRuns: len(items), ExpectedRuns: expected, TotalCostUSD: totalCost, TotalRequests: totalRequests, TotalPromptTokens: promptTokens, TotalCompletionTokens: completionTokens, TotalBenchmarkWallSeconds: wallSeconds, MedianRunP50RelayLatencyMS: medianInt64(p50s), MedianRunP95RelayLatencyMS: medianInt64(p95s), ResponseProviderAttribution: attribution}
	if expected > 0 {
		summary.RunSuccessRate = float64(len(items)) / float64(expected)
	}
	if len(items) > 0 {
		summary.MeanCostPerRunUSD = totalCost / float64(len(items))
	}
	if totalRequests > 0 {
		summary.RelayErrorRate = float64(relayErrors) / float64(totalRequests)
	}
	if wallSeconds > 0 {
		summary.EffectiveCompletionTokPerS = float64(completionTokens) / wallSeconds
	}
	for _, agentItems := range byAgent {
		summary.Agents = append(summary.Agents, summarizeAgent(agentItems))
	}
	sort.Slice(summary.Agents, func(i, j int) bool { return summary.Agents[i].Rank < summary.Agents[j].Rank })
	var absDeltas, signedDeltas, stddevs []float64
	for _, agent := range summary.Agents {
		absDeltas = append(absDeltas, agent.AbsoluteCompositeDelta)
		signedDeltas = append(signedDeltas, agent.CompositeDelta)
		stddevs = append(stddevs, agent.OpenRouterCompositeStdDev)
	}
	summary.MeanAbsoluteCompositeDelta = mean(absDeltas)
	summary.MeanSignedCompositeDelta = mean(signedDeltas)
	summary.MeanCrossRunCompositeStdDev = mean(stddevs)
	for category, acc := range categories {
		chutesMean := acc.ChutesWeighted / float64(acc.Count)
		openRouterMean := acc.OpenRouterWeighted / float64(acc.Count)
		summary.PerCategoryChanges = append(summary.PerCategoryChanges, categorySummary{Category: category, Count: acc.Count, ChutesMean: chutesMean, OpenRouterMean: openRouterMean, Delta: openRouterMean - chutesMean})
	}
	sort.Slice(summary.PerCategoryChanges, func(i, j int) bool {
		return summary.PerCategoryChanges[i].Category < summary.PerCategoryChanges[j].Category
	})
	return summary
}

func baselineCategoryMean(agent baselineAgent, category string) float64 {
	for _, item := range agent.ChutesCategories {
		if item.Category == category {
			return item.Mean
		}
	}
	return 0
}

func summarizeAgent(items []observation) agentSummary {
	sort.Slice(items, func(i, j int) bool { return items[i].Run < items[j].Run })
	agent := items[0].Agent
	var composites, tools, memories, medians, costs []float64
	var runResults []agentRunSummary
	for _, item := range items {
		composites = append(composites, item.Score.Composite)
		tools = append(tools, item.Score.ToolMean)
		memories = append(memories, item.Score.MemoryMean)
		medians = append(medians, float64(item.Score.MedianMS))
		costs = append(costs, item.Relay.CostUSD)
		relayErrors := max(item.Relay.HTTPErrorResponses, item.Relay.APIErrorResponses) + item.Relay.UpstreamErrors
		runResults = append(runResults, agentRunSummary{
			Run: item.Run, Composite: item.Score.Composite, ToolMean: item.Score.ToolMean,
			MemoryMean: item.Score.MemoryMean, MedianMS: item.Score.MedianMS,
			CostUSD: item.Relay.CostUSD, Requests: item.Relay.Requests,
			RelayErrors: relayErrors, P50RelayLatencyMS: item.Relay.P50LatencyMS,
			P95RelayLatencyMS:           item.Relay.P95LatencyMS,
			BenchmarkWallSeconds:        item.WallTime.Seconds(),
			ResponseProviderAttribution: item.Relay.ResponseProviders,
		})
	}
	compositeMean := mean(composites)
	toolMean := mean(tools)
	memoryMean := mean(memories)
	medianMean := mean(medians)
	delta := compositeMean - agent.ChutesComposite
	latencyRatio := 0.0
	if agent.ChutesMedianMS > 0 {
		latencyRatio = medianMean / float64(agent.ChutesMedianMS)
	}
	return agentSummary{Rank: agent.Rank, AgentID: agent.AgentID, AgentName: agent.AgentName, AgentVersion: agent.AgentVersion, ArtifactSHA256: agent.ArtifactSHA256, DatasetSeed: agent.DatasetSeed, DatasetSHA256: agent.DatasetSHA256, Runs: len(items), ChutesComposite: agent.ChutesComposite, OpenRouterCompositeMean: compositeMean, CompositeDelta: delta, AbsoluteCompositeDelta: math.Abs(delta), OpenRouterCompositeStdDev: sampleStdDev(composites), OpenRouterCompositeMin: minFloat(composites), OpenRouterCompositeMax: maxFloat(composites), ChutesToolMean: agent.ChutesToolMean, OpenRouterToolMean: toolMean, ToolMeanDelta: toolMean - agent.ChutesToolMean, ChutesMemoryMean: agent.ChutesMemoryMean, OpenRouterMemoryMean: memoryMean, MemoryMeanDelta: memoryMean - agent.ChutesMemoryMean, ChutesMedianMS: agent.ChutesMedianMS, OpenRouterMedianMSMean: medianMean, OpenRouterVsChutesLatency: latencyRatio, TotalCostUSD: sum(costs), RunResults: runResults}
}

func readJSON[T any](path string) T {
	var value T
	raw, err := os.ReadFile(path)
	if err != nil {
		fatal(err)
	}
	if err := json.Unmarshal(raw, &value); err != nil {
		fatal(fmt.Errorf("%s: %w", path, err))
	}
	return value
}

func mean(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	return sum(values) / float64(len(values))
}

func sum(values []float64) float64 {
	var total float64
	for _, value := range values {
		total += value
	}
	return total
}

func sampleStdDev(values []float64) float64 {
	if len(values) < 2 {
		return 0
	}
	avg := mean(values)
	var squared float64
	for _, value := range values {
		delta := value - avg
		squared += delta * delta
	}
	return math.Sqrt(squared / float64(len(values)-1))
}

func minFloat(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	result := values[0]
	for _, value := range values[1:] {
		result = min(result, value)
	}
	return result
}

func maxFloat(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	result := values[0]
	for _, value := range values[1:] {
		result = max(result, value)
	}
	return result
}

func medianInt64(values []int64) int64 {
	if len(values) == 0 {
		return 0
	}
	copyValues := append([]int64(nil), values...)
	sort.Slice(copyValues, func(i, j int) bool { return copyValues[i] < copyValues[j] })
	return copyValues[len(copyValues)/2]
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "provider-score-analyze:", err)
	os.Exit(1)
}
