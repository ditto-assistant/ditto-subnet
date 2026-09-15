// Command mixaudit is the per-seed memory-mix histogram behind the Bench v13
// envelope (issues #1829/#1830). It extends the structural family counts
// cmd/vstudy reports with the axes the envelope is defined on: semantic domain
// and sub-domain, answer kind, head operation, monetary exposure (direct answer
// kinds AND typed list items, weighted by their fraction of case credit),
// arithmetic, computed-vs-verbatim, language, twin/metamorphic relation, and
// gate exposure. Output is JSON (default) or the docs/v9-family-mix-study.md
// table format (-markdown). It exits non-zero on any unclassified answer kind,
// list-item kind, or family.
//
// The v13 caps and floors (mixaudit.V13Envelope) are always evaluated and
// reported; -enforce turns a violation on any seed into a non-zero exit. The
// pass-off regression proof is the v12 public seed:
//
//	go run ./cmd/mixaudit -bench-version 12 -seed 123456789 -run-size full
//
// which reproduces issue #1529's 117 direct money cases, 143 money-bearing
// cases, and 127.83/251 = 50.9% money weight.
//
// -gih attaches the per-seed generator-inverse family-identification rate from
// a cmd/parserprobe -json report as the gih_parse_rate column.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/internal/mixaudit"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	version := flag.Int("bench-version", newestSupportedVersion(), "benchmark contract version (default: newest supported)")
	seed := flag.Int64("seed", 0, "single dataset seed (overrides -seeds/-first-seed)")
	seeds := flag.Int("seeds", 40, "number of seeds (first-seed .. first-seed+n-1)")
	firstSeed := flag.Int64("first-seed", 1, "first seed of the sweep")
	runSize := flag.String("run-size", "full", "small, medium, or full")
	markdown := flag.Bool("markdown", false, "render the v9-study table format instead of JSON")
	perSeed := flag.Bool("per-seed", false, "include every per-seed report in the JSON output")
	cases := flag.Bool("cases", false, "include every classified case in the per-seed reports (large)")
	enforce := flag.Bool("enforce", false, "exit 3 when any seed violates the v13 envelope")
	gihPath := flag.String("gih", "", "cmd/parserprobe -json report supplying the gih_parse_rate column")
	out := flag.String("out", "", "write output here (default: stdout)")
	flag.Parse()

	if !protocol.SupportedBenchVersion(*version) {
		fmt.Fprintf(os.Stderr, "mixaudit: unsupported bench_version %d\n", *version)
		os.Exit(2)
	}
	prof, ok := gen.ProfileForVersion(*runSize, *version)
	if !ok {
		fmt.Fprintf(os.Stderr, "mixaudit: unsupported run size %q for v%d\n", *runSize, *version)
		os.Exit(2)
	}
	var list []int64
	if isFlagSet("seed") {
		list = []int64{*seed}
	} else {
		for i := 0; i < *seeds; i++ {
			list = append(list, *firstSeed+int64(i))
		}
	}
	gih := map[int64]float64{}
	if *gihPath != "" {
		var err error
		gih, err = loadGIH(*gihPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "mixaudit: %v\n", err)
			os.Exit(2)
		}
	}

	reports := make([]mixaudit.SeedReport, 0, len(list))
	for _, s := range list {
		artifact, err := gen.GenerateDataset(s, prof, *version)
		if err != nil {
			fmt.Fprintf(os.Stderr, "mixaudit: generate v%d seed %d: %v\n", *version, s, err)
			os.Exit(1)
		}
		report, err := mixaudit.Audit(artifact, *runSize, *cases)
		if err != nil {
			fmt.Fprintf(os.Stderr, "mixaudit: seed %d: %v\n", s, err)
			os.Exit(1)
		}
		if rate, ok := gih[s]; ok {
			report.GIHParseRate = &rate
		}
		reports = append(reports, report)
	}
	summary := mixaudit.Summarize(reports, mixaudit.V13Envelope)

	var payload []byte
	if *markdown {
		payload = []byte(summary.Markdown(mixaudit.V13Envelope))
	} else {
		doc := map[string]any{"summary": summary}
		if *perSeed || len(reports) == 1 {
			doc["seeds"] = reports
		}
		var err error
		payload, err = json.MarshalIndent(doc, "", "  ")
		if err != nil {
			fmt.Fprintf(os.Stderr, "mixaudit: %v\n", err)
			os.Exit(1)
		}
		payload = append(payload, '\n')
	}
	if *out != "" {
		if err := os.WriteFile(*out, payload, 0o644); err != nil {
			fmt.Fprintf(os.Stderr, "mixaudit: %v\n", err)
			os.Exit(1)
		}
	} else if _, err := os.Stdout.Write(payload); err != nil {
		os.Exit(1)
	}
	if len(reports) == 1 {
		r := reports[0]
		fmt.Fprintf(os.Stderr, "v%d %s seed %d: %d memory cases; direct money %d; money-bearing %d; money weight %.2f/%d = %.1f%%\n",
			r.BenchVersion, r.RunSize, r.Seed, r.MemoryCases, r.DirectMoneyCases, r.MoneyBearingCases, r.MoneyWeight, int(r.Weight), 100*r.MoneyShare)
	}
	if *enforce && summary.SeedsViolating > 0 {
		fmt.Fprintf(os.Stderr, "mixaudit: %d of %d seeds violate the v13 envelope\n", summary.SeedsViolating, len(reports))
		os.Exit(3)
	}
}

// gihReport is the subset of the cmd/parserprobe JSON report this command reads.
type gihReport struct {
	Seeds []struct {
		Seed           int64   `json:"seed"`
		FamilyIDRate   float64 `json:"family_id_rate"`
		MemoryFamilyID float64 `json:"memory_family_id_rate"`
	} `json:"seeds"`
}

func loadGIH(path string) (map[int64]float64, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var doc gihReport
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	out := make(map[int64]float64, len(doc.Seeds))
	for _, s := range doc.Seeds {
		out[s.Seed] = s.MemoryFamilyID
	}
	return out, nil
}

func isFlagSet(name string) bool {
	set := false
	flag.Visit(func(f *flag.Flag) {
		if f.Name == name {
			set = true
		}
	})
	return set
}

// newestSupportedVersion scans upward from the display version so the default
// tracks whatever contract this module can generate, never a hard-coded pin.
func newestSupportedVersion() int {
	newest := protocol.CurrentBenchVersion
	for v := protocol.CurrentBenchVersion; v < protocol.CurrentBenchVersion+64; v++ {
		if protocol.SupportedBenchVersion(v) {
			newest = v
		}
	}
	return newest
}
