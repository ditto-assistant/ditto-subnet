// Command provider-analyze merges completed provider-cert reports without
// repeating paid cells. It recalculates summaries from the preserved raw run
// observations and rejects incompatible model/schema inputs.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"

	"github.com/ditto-assistant/dittobench-api/internal/providercert"
)

func main() {
	output := flag.String("output", "", "write merged JSON to this file instead of stdout")
	flag.Parse()
	if flag.NArg() < 1 {
		fail(fmt.Errorf("at least one provider-cert report is required"))
	}
	var merged providercert.Report
	runs := map[int]bool{}
	cells := map[string]bool{}
	for i, path := range flag.Args() {
		file, err := os.Open(path)
		if err != nil {
			fail(err)
		}
		var report providercert.Report
		err = json.NewDecoder(file).Decode(&report)
		_ = file.Close()
		if err != nil {
			fail(fmt.Errorf("decode %s: %w", path, err))
		}
		if report.SchemaVersion != providercert.SchemaVersion {
			fail(fmt.Errorf("%s has schema_version %d", path, report.SchemaVersion))
		}
		if i == 0 {
			merged = report
			merged.Results = nil
		} else if report.Model != merged.Model {
			fail(fmt.Errorf("%s model %q does not match %q", path, report.Model, merged.Model))
		}
		if report.StartedAt.Before(merged.StartedAt) {
			merged.StartedAt = report.StartedAt
		}
		if report.FinishedAt.After(merged.FinishedAt) {
			merged.FinishedAt = report.FinishedAt
		}
		for _, result := range report.Results {
			cell := fmt.Sprintf("%s/%s/%d", result.ProviderSlug, result.Scenario, result.Run)
			if cells[cell] {
				fail(fmt.Errorf("duplicate completed cell %s", cell))
			}
			cells[cell] = true
			runs[result.Run] = true
			merged.Results = append(merged.Results, result)
		}
	}
	merged.RunOffset = 0
	merged.RunsPerScenario = len(runs)
	sort.Slice(merged.Results, func(i, j int) bool {
		a, b := merged.Results[i], merged.Results[j]
		if a.ProviderSlug != b.ProviderSlug {
			return a.ProviderSlug < b.ProviderSlug
		}
		if a.Run != b.Run {
			return a.Run < b.Run
		}
		return a.Scenario < b.Scenario
	})
	merged.Summary = providercert.Summarize(merged.Results)
	out := os.Stdout
	if *output != "" {
		file, err := os.Create(*output)
		if err != nil {
			fail(err)
		}
		defer file.Close()
		out = file
	}
	enc := json.NewEncoder(out)
	enc.SetIndent("", "  ")
	if err := enc.Encode(merged); err != nil {
		fail(err)
	}
}

func fail(err error) {
	_, _ = fmt.Fprintln(os.Stderr, "provider-analyze:", err)
	os.Exit(1)
}
