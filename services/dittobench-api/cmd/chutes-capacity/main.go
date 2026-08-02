// Command chutes-capacity ranks an explicit set of public Chutes models from
// repeated utilization snapshots. It is intentionally offline: snapshot
// collection and scoring are separate so a load test cannot bias its own
// capacity evidence.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
	"strings"
)

type snapshotRow struct {
	Name                  string  `json:"name"`
	Timestamp             string  `json:"timestamp"`
	ActiveInstances       float64 `json:"active_instance_count"`
	TotalInstances        float64 `json:"total_instance_count"`
	Scalable              bool    `json:"scalable"`
	ScaleAllowance        float64 `json:"scale_allowance"`
	UtilizationCurrent    float64 `json:"utilization_current"`
	Utilization5M         float64 `json:"utilization_5m"`
	Utilization1H         float64 `json:"utilization_1h"`
	TotalRequests1H       float64 `json:"total_requests_1h"`
	CompletedRequests1H   float64 `json:"completed_requests_1h"`
	RateLimitedRequests1H float64 `json:"rate_limited_requests_1h"`
}

type candidate struct {
	Model                       string   `json:"model"`
	SnapshotTimestamps          []string `json:"snapshot_timestamps"`
	SnapshotCount               int      `json:"snapshot_count"`
	MedianActiveInstances       float64  `json:"median_active_instances"`
	MedianTotalInstances        float64  `json:"median_total_instances"`
	Scalable                    bool     `json:"scalable"`
	MedianScaleAllowance        float64  `json:"median_scale_allowance"`
	MedianUtilizationCurrent    float64  `json:"median_utilization_current"`
	MedianUtilization5M         float64  `json:"median_utilization_5m"`
	MedianUtilization1H         float64  `json:"median_utilization_1h"`
	ConservativeUtilization     float64  `json:"conservative_utilization"`
	ConservativeHeadroom        float64  `json:"conservative_headroom"`
	MedianCompletedRequests1H   float64  `json:"median_completed_requests_1h"`
	MedianRateLimitedRequests1H float64  `json:"median_rate_limited_requests_1h"`
	MedianCompletionRatio1H     float64  `json:"median_completion_ratio_1h"`
	CapacityScore               float64  `json:"capacity_score"`
}

type report struct {
	SchemaVersion int         `json:"schema_version"`
	Method        scoreMethod `json:"method"`
	Candidates    []candidate `json:"candidates"`
}

type scoreMethod struct {
	FleetWithAllowanceWeight float64 `json:"fleet_with_allowance_weight"`
	ScaleAllowanceWeight     float64 `json:"scale_allowance_weight"`
	HeadroomWeight           float64 `json:"headroom_weight"`
	SustainedLoadWeight      float64 `json:"sustained_load_weight"`
	CompletionRatioWeight    float64 `json:"completion_ratio_weight"`
	UtilizationRule          string  `json:"utilization_rule"`
	AggregationRule          string  `json:"aggregation_rule"`
}

func main() {
	modelsFlag := flag.String("models", "", "comma-separated exact Chutes model names")
	output := flag.String("output", "", "JSON output path; stdout when empty")
	flag.Parse()
	models := splitModels(*modelsFlag)
	if len(models) == 0 || flag.NArg() == 0 {
		fail(errors.New("-models and at least one snapshot path are required"))
	}
	rep, err := analyze(flag.Args(), models)
	if err != nil {
		fail(err)
	}
	var destination = os.Stdout
	if *output != "" {
		destination, err = os.Create(*output)
		if err != nil {
			fail(err)
		}
		defer destination.Close()
	}
	encoder := json.NewEncoder(destination)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(rep); err != nil {
		fail(err)
	}
}

func analyze(paths, models []string) (report, error) {
	selected := make(map[string]bool, len(models))
	byModel := make(map[string][]snapshotRow, len(models))
	for _, model := range models {
		if selected[model] {
			return report{}, fmt.Errorf("duplicate selected model %q", model)
		}
		selected[model] = true
	}
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			return report{}, err
		}
		var rows []snapshotRow
		if err := json.Unmarshal(raw, &rows); err != nil {
			return report{}, fmt.Errorf("decode %s: %w", path, err)
		}
		seen := map[string]bool{}
		for _, row := range rows {
			if !selected[row.Name] {
				continue
			}
			if seen[row.Name] {
				return report{}, fmt.Errorf("model %q occurs more than once in %s", row.Name, path)
			}
			seen[row.Name] = true
			byModel[row.Name] = append(byModel[row.Name], row)
		}
	}

	rep := report{SchemaVersion: 1, Method: scoreMethod{
		FleetWithAllowanceWeight: 30, ScaleAllowanceWeight: 5,
		HeadroomWeight: 35, SustainedLoadWeight: 20,
		CompletionRatioWeight: 10,
		UtilizationRule:       "per snapshot max(current,5m,1h); median across snapshots",
		AggregationRule:       "component min-max anchors are maxima across selected candidates",
	}}
	for _, model := range models {
		rows := byModel[model]
		if len(rows) != len(paths) {
			return report{}, fmt.Errorf("model %q found in %d/%d snapshots", model, len(rows), len(paths))
		}
		rep.Candidates = append(rep.Candidates, summarize(model, rows))
	}
	score(rep.Candidates)
	sort.SliceStable(rep.Candidates, func(i, j int) bool {
		return rep.Candidates[i].CapacityScore > rep.Candidates[j].CapacityScore
	})
	return rep, nil
}

func summarize(model string, rows []snapshotRow) candidate {
	c := candidate{Model: model, SnapshotCount: len(rows), Scalable: true}
	var active, total, allowance, current, fiveMinute, oneHour []float64
	var conservative, completed, rateLimited, completionRatio []float64
	for _, row := range rows {
		c.SnapshotTimestamps = append(c.SnapshotTimestamps, row.Timestamp)
		c.Scalable = c.Scalable && row.Scalable
		active = append(active, row.ActiveInstances)
		total = append(total, row.TotalInstances)
		allowance = append(allowance, row.ScaleAllowance)
		current = append(current, row.UtilizationCurrent)
		fiveMinute = append(fiveMinute, row.Utilization5M)
		oneHour = append(oneHour, row.Utilization1H)
		conservative = append(conservative, max(row.UtilizationCurrent, row.Utilization5M, row.Utilization1H))
		completed = append(completed, row.CompletedRequests1H)
		rateLimited = append(rateLimited, row.RateLimitedRequests1H)
		ratio := 0.0
		if row.TotalRequests1H > 0 {
			ratio = row.CompletedRequests1H / row.TotalRequests1H
		}
		completionRatio = append(completionRatio, ratio)
	}
	sort.Strings(c.SnapshotTimestamps)
	c.MedianActiveInstances = median(active)
	c.MedianTotalInstances = median(total)
	c.MedianScaleAllowance = median(allowance)
	c.MedianUtilizationCurrent = median(current)
	c.MedianUtilization5M = median(fiveMinute)
	c.MedianUtilization1H = median(oneHour)
	c.ConservativeUtilization = median(conservative)
	c.ConservativeHeadroom = clamp01(1 - c.ConservativeUtilization)
	c.MedianCompletedRequests1H = median(completed)
	c.MedianRateLimitedRequests1H = median(rateLimited)
	c.MedianCompletionRatio1H = clamp01(median(completionRatio))
	return c
}

func score(candidates []candidate) {
	var maxFleet, maxAllowance, maxSustained float64
	for _, c := range candidates {
		maxFleet = max(maxFleet, c.MedianActiveInstances+c.MedianScaleAllowance)
		maxAllowance = max(maxAllowance, c.MedianScaleAllowance)
		maxSustained = max(maxSustained, math.Log1p(c.MedianCompletedRequests1H))
	}
	for index := range candidates {
		c := &candidates[index]
		points := normalized(c.MedianActiveInstances+c.MedianScaleAllowance, maxFleet) * 30
		points += normalized(c.MedianScaleAllowance, maxAllowance) * 5
		points += c.ConservativeHeadroom * 35
		points += normalized(math.Log1p(c.MedianCompletedRequests1H), maxSustained) * 20
		points += c.MedianCompletionRatio1H * 10
		c.CapacityScore = math.Round(points*100) / 100
	}
}

func splitModels(raw string) []string {
	var models []string
	for _, model := range strings.Split(raw, ",") {
		if model = strings.TrimSpace(model); model != "" {
			models = append(models, model)
		}
	}
	return models
}

func median(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	middle := len(sorted) / 2
	if len(sorted)%2 == 1 {
		return sorted[middle]
	}
	return (sorted[middle-1] + sorted[middle]) / 2
}

func normalized(value, maximum float64) float64 {
	if maximum == 0 {
		return 0
	}
	return clamp01(value / maximum)
}

func clamp01(value float64) float64 {
	return min(1, max(0, value))
}

func fail(err error) {
	_, _ = fmt.Fprintln(os.Stderr, "chutes-capacity:", err)
	os.Exit(1)
}
