// Command familypanel joins executed v14 runs to the generator's family labels
// and judges every family with familygate's thresholds (internal/familypanel).
//
// Usage:
//
//	familypanel -runs runs.json [-json] [-bench-version 14] [-run-size full]
//	familypanel -plan -seeds 123,456   # families each seed's dataset contains
//
// runs.json is a JSON array of {run, owner, seed, dataset_sha256, per_case},
// where per_case is the scorer's per-case report (case_id and score are read).
// Every run's dataset is regenerated from its seed and must match its digest.
// The tool reads no network and spends nothing.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/familygate"
	"github.com/ditto-assistant/dittobench-datagen/internal/familypanel"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "familypanel:", err)
		os.Exit(1)
	}
}

func run(args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("familypanel", flag.ContinueOnError)
	runsPath := fs.String("runs", "", "JSON array of executed runs")
	plan := fs.Bool("plan", false, "print the families each -seeds dataset contains")
	seedList := fs.String("seeds", "", "comma-separated seeds for -plan")
	benchVersion := fs.Int("bench-version", protocol.BenchVersionV14, "benchmark contract")
	runSize := fs.String("run-size", "full", "dataset profile")
	asJSON := fs.Bool("json", false, "emit JSON")
	minOwners := fs.Int("min-panel-owners", familypanel.DefaultOptions().MinPanelOwners, "fewest owners with cases of a family")
	minCases := fs.Int("min-owner-cases", familypanel.DefaultOptions().MinOwnerCases, "fewest cases per panel owner")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cache := map[int64]familypanel.Labels{}
	labelsFor := func(seed int64) (familypanel.Labels, error) {
		if l, ok := cache[seed]; ok {
			return l, nil
		}
		l, err := familypanel.LabelDataset(seed, *runSize, *benchVersion)
		if err == nil {
			cache[seed] = l
		}
		return l, err
	}
	if *plan {
		for _, field := range strings.Split(*seedList, ",") {
			seed, err := strconv.ParseInt(strings.TrimSpace(field), 10, 64)
			if err != nil {
				return fmt.Errorf("seed %q: %w", field, err)
			}
			l, err := labelsFor(seed)
			if err != nil {
				return err
			}
			fmt.Fprintf(stdout, "%d\t%s\t%s\n", seed, l.DatasetSHA256, strings.Join(l.Families(), ","))
		}
		return nil
	}
	if *runsPath == "" {
		return fmt.Errorf("-runs or -plan is required")
	}
	raw, err := os.ReadFile(*runsPath)
	if err != nil {
		return err
	}
	var runs []familypanel.Run
	if err := json.Unmarshal(raw, &runs); err != nil {
		return fmt.Errorf("decode runs: %w", err)
	}
	p := familygate.DefaultParams()
	results, err := familypanel.Evaluate(runs, labelsFor, p, familypanel.Options{MinPanelOwners: *minOwners, MinOwnerCases: *minCases})
	if err != nil {
		return err
	}
	if *asJSON {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(results)
	}
	fmt.Fprintf(stdout, "Runs: %d. Gate (worst case over every coupling): >= %d seeds, trivial <= %.2f, unsolved (< %d owners at score >= %.2f) <= %.2f, at-threshold share <= %.2f; panel >= %d owners with >= %d cases each.\n\n",
		len(runs), p.MinRounds, p.MaxTrivialRate, p.MinSolverOwners, p.SolveThreshold, p.MaxUnsolvedRate, p.MaxAtThresholdRate, *minOwners, *minCases)
	fmt.Fprintln(stdout, "| family | ledger | seeds | cases | owners | min owner cases | owner solve rates | trivial max | unsolved max | trivial indep | unsolved indep | at threshold | verdict |")
	fmt.Fprintln(stdout, "|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---|")
	for _, r := range results {
		rates := make([]string, 0, len(r.Owners))
		for _, o := range r.Owners {
			rates = append(rates, fmt.Sprintf("%d/%d", o.Solved, o.Cases))
		}
		verdict := r.Verdict
		if len(r.Reasons) > 0 {
			verdict += " (" + strings.Join(r.Reasons, "; ") + ")"
		}
		fmt.Fprintf(stdout, "| %s | %s | %d | %d | %d | %d | %s | %.2f | %.2f | %.2f | %.2f | %.2f | %s |\n",
			r.Family, r.Status, r.Seeds, r.Cases, len(r.Owners), r.MinOwnerCases, strings.Join(rates, " "),
			r.TrivialMax, r.UnsolvedMax, r.TrivialIndep, r.UnsolvedIndep, r.AtThresholdRate, verdict)
	}
	return nil
}
