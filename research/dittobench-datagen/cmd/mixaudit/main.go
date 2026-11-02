// Command mixaudit is the deterministic memory-mix histogram behind the Bench
// v13 monetary-exposure cap (issues #1529 / #1830 / #1848). For every seed it
// generates the dataset, classifies each memory case (gen.AuditMix), evaluates
// the #1529 gate (gen.MixGateV13), and prints one row per seed plus an
// aggregate, as a markdown table (default) or JSON.
//
// Usage:
//
//	mixaudit -bench-version 13 -seeds 40 -run-size full
//	mixaudit -bench-version 12 -seeds 1 -first-seed 123456789 -json
//	mixaudit -bench-version 13 -seeds 40 -gate        # exit 1 on any violation
//
// -bench-version defaults to the newest supported contract. The tool never
// changes generation bytes; it reads the artifact the validator would score.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	seeds := flag.Int("seeds", 40, "number of dataset seeds")
	firstSeed := flag.Int64("first-seed", 1, "first seed (seeds are first..first+n-1)")
	runSize := flag.String("run-size", "full", "dataset profile: small, medium, or full")
	benchVersion := flag.Int("bench-version", protocol.NewestSupportedBenchVersion(), "benchmark contract to audit")
	asJSON := flag.Bool("json", false, "emit JSON instead of markdown")
	classes := flag.Bool("classes", false, "with -json, include the per-case classification")
	gate := flag.Bool("gate", false, "exit 1 when any seed violates the v13 gate")
	flag.Parse()

	if !protocol.SupportedBenchVersion(*benchVersion) {
		fmt.Fprintf(os.Stderr, "unsupported bench version %d\n", *benchVersion)
		os.Exit(2)
	}
	profile, ok := gen.ProfileForVersion(*runSize, *benchVersion)
	if !ok {
		fmt.Fprintln(os.Stderr, "run size: must be small, medium, or full")
		os.Exit(2)
	}

	rows := make([]row, 0, *seeds)
	violating := 0
	aggregate := map[string]float64{}
	families := map[string]int{}
	var interim, interimGenerators []string
	for i := 0; i < *seeds; i++ {
		seed := *firstSeed + int64(i)
		artifact, err := gen.GenerateDataset(seed, profile, *benchVersion)
		if err != nil {
			fmt.Fprintf(os.Stderr, "seed %d: %v\n", seed, err)
			os.Exit(1)
		}
		audit, err := gen.AuditMix(artifact)
		if err != nil {
			fmt.Fprintf(os.Stderr, "seed %d: %v\n", seed, err)
			os.Exit(1)
		}
		interim = audit.InterimSlots
		interimGenerators = audit.InterimGenerators
		summary := audit.Summarize(gen.MixGateV13)
		if len(summary.Violations) > 0 {
			violating++
		}
		aggregate["money_share"] += summary.MoneyShare
		aggregate["money_cases"] += float64(summary.MoneyCases)
		aggregate["arithmetic_share"] += summary.ArithmeticShare
		aggregate["computed_money_share"] += summary.ComputedMoneyShare
		aggregate["personal_share"] += summary.PersonalShare
		aggregate["business_share"] += summary.BusinessShare
		aggregate["abstention_share"] += summary.AbstentionShare
		aggregate["twin_share"] += summary.TwinShare
		aggregate["gate_exposed_share"] += summary.GateExposedShare
		aggregate["cascade_share"] += summary.CascadeShare
		for family, count := range audit.Families {
			families[family] += count
		}
		r := row{Summary: summary}
		if *asJSON && *classes {
			copyAudit := audit
			r.Audit = &copyAudit
		} else if *asJSON {
			copyAudit := audit
			copyAudit.Classes = nil
			r.Audit = &copyAudit
		}
		rows = append(rows, r)
	}
	n := float64(len(rows))
	for key := range aggregate {
		aggregate[key] /= n
	}

	if *asJSON {
		out := map[string]any{
			"bench_version": *benchVersion, "run_size": *runSize, "seeds": len(rows),
			"interim_slots": interim, "interim_generators": interimGenerators, "violating_seeds": violating,
			"mean": aggregate, "families_total": families, "rows": rows,
		}
		b, _ := json.MarshalIndent(out, "", "  ")
		fmt.Println(string(b))
	} else {
		printMarkdown(*benchVersion, *runSize, rows, aggregate, families, interim, interimGenerators, violating)
	}
	if *gate && violating > 0 {
		os.Exit(1)
	}
}

// row is one seed's report: the gate summary plus, with -json, the audit.
type row struct {
	Summary gen.MixAuditSummary `json:"summary"`
	Audit   *gen.MixAudit       `json:"audit,omitempty"`
}

func printMarkdown(benchVersion int, runSize string, rows []row, aggregate map[string]float64, families map[string]int, interim, interimGenerators []string, violating int) {
	fmt.Printf("# Bench v%d %s memory mix, %d seeds\n\n", benchVersion, runSize, len(rows))
	if len(interim) > 0 {
		fmt.Printf("Interim slots (filled by non-monetary world questions until their generator lands): %s\n\n", strings.Join(interim, ", "))
	}
	if len(interimGenerators) > 0 {
		fmt.Printf("Interim generators (slot count final, cases still from a monetary v12-era generator): %s\n\n", strings.Join(interimGenerators, ", "))
	}
	fmt.Println("| seed | cases | money wt | money cases | $ programs | arith | $ of computed | personal | business | max sub-domain | max kind x op | abstain | twin | gate-exposed | cascade | violations |")
	fmt.Println("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |")
	for _, r := range rows {
		s := r.Summary
		fmt.Printf("| %d | %d | %.3f | %d | %d | %.3f | %.3f | %.3f | %.3f | %s %.3f | %s %.3f | %.3f | %.3f | %.3f | %.3f | %d |\n",
			s.Seed, s.Cases, s.MoneyShare, s.MoneyCases, s.MonetaryOpenPrograms, s.ArithmeticShare, s.ComputedMoneyShare,
			s.PersonalShare, s.BusinessShare, s.MaxSubDomain, s.MaxSubDomainShare, s.MaxKindOperation, s.MaxKindOperationShare,
			s.AbstentionShare, s.TwinShare, s.GateExposedShare, s.CascadeShare, len(s.Violations))
	}
	fmt.Println()
	fmt.Printf("Mean over %d seeds: money %.4f (cases %.1f), arithmetic %.4f, money-of-computed %.4f, personal %.4f, business %.4f, abstention %.4f, twin %.4f, gate-exposed %.4f, cascade %.4f. Seeds violating the v13 gate: %d/%d.\n\n",
		len(rows), aggregate["money_share"], aggregate["money_cases"], aggregate["arithmetic_share"], aggregate["computed_money_share"],
		aggregate["personal_share"], aggregate["business_share"], aggregate["abstention_share"], aggregate["twin_share"],
		aggregate["gate_exposed_share"], aggregate["cascade_share"], violating, len(rows))
	if len(rows) > 0 {
		fmt.Println("Slots (first seed):")
		for _, slot := range gen.V13SlotOrder {
			if count, ok := rows[0].Summary.Slots[slot]; ok {
				fmt.Printf("- %s: %d\n", slot, count)
			}
		}
		fmt.Println()
		fmt.Println("Violations (first seed):")
		for _, v := range rows[0].Summary.Violations {
			fmt.Printf("- %s\n", v)
		}
		if len(rows[0].Summary.Violations) == 0 {
			fmt.Println("- none")
		}
		fmt.Println()
	}
	fmt.Println("| family | total across seeds |")
	fmt.Println("| --- | ---: |")
	names := make([]string, 0, len(families))
	for name := range families {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		fmt.Printf("| %s | %d |\n", name, families[name])
	}
}
