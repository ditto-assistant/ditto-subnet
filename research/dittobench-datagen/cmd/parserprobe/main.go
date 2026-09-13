// Command parserprobe is the generator-inverse harness (GIH, strategy N13)
// and the seed-trained router (N14): the honest model-free adversaries the
// Bench v13 surface work is measured against (issue #1829).
//
// It reads only what a harness sees on the wire — the /seed records, the
// staged questions, the tool prompts — assembles a parser from the repository's
// own frames and banks (datagen.ToolSurfacesForVersion, the world record
// frames, the story fact renderings, the v12 program forms, the family-compiler
// and divergence frames), recovers (family, slots) for every question, applies
// the public oracle arithmetic, launders the value through one "reply exactly"
// completion, and is graded by the real deterministic grader. It reports the
// per-seed and per-slice family-identification rate, answer rate, and composite
// (0.5*tool_mean + 0.5*memory_mean) for both adversaries.
//
// On a public pass-off artifact the GIH scores near the oracle by construction.
// That is the published baseline, not a failure: the private surface pass is
// what has to pull it under the starter-kit ceiling documented in
// docs/bench-versions.md. Ceilings stay report-only until that owner decision
// lands; -artifact probes a surface-passed artifact JSON written by cmd/generate
// (or the sibling salted pass) so the two rows can be compared.
//
//	go run ./cmd/parserprobe -bench-version 12 -seeds 40 -run-size full -json
//	go run ./cmd/parserprobe -bench-version 12 -seeds 40 -router-seeds 10000 -json
//	go run ./cmd/parserprobe -artifact /tmp/salted.json -json
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/internal/parserprobe"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	version := flag.Int("bench-version", newestSupportedVersion(), "benchmark contract version (default: newest supported)")
	seeds := flag.Int("seeds", 40, "number of probe seeds (first-seed .. first-seed+n-1)")
	firstSeed := flag.Int64("first-seed", 1, "first probe seed")
	runSize := flag.String("run-size", "full", "small, medium, or full")
	routerSeeds := flag.Int("router-seeds", 0, "train the N14 router on this many locally generated seeds (0 = GIH only; 10000 is the documented N14 configuration)")
	routerFirst := flag.Int64("router-first-seed", 0, "first router training seed (default: disjoint range after the probe seeds)")
	artifactPaths := flag.String("artifact", "", "comma-separated artifact JSON paths to probe instead of generating (surface-passed artifacts)")
	asJSON := flag.Bool("json", false, "emit the full JSON report on stdout")
	out := flag.String("out", "", "write the JSON report here")
	flag.Parse()

	opts := parserprobe.Options{
		BenchVersion: *version, RunSize: *runSize, FirstSeed: *firstSeed, Seeds: *seeds,
		RouterSeeds: *routerSeeds, RouterFirstSeed: *routerFirst,
	}
	if *artifactPaths != "" {
		for _, path := range strings.Split(*artifactPaths, ",") {
			path = strings.TrimSpace(path)
			if path == "" {
				continue
			}
			raw, err := os.ReadFile(path)
			if err != nil {
				fmt.Fprintf(os.Stderr, "parserprobe: %v\n", err)
				os.Exit(2)
			}
			var a gen.DatasetArtifact
			if err := json.Unmarshal(raw, &a); err != nil {
				fmt.Fprintf(os.Stderr, "parserprobe: parse %s: %v\n", path, err)
				os.Exit(2)
			}
			if !protocol.SupportedBenchVersion(a.BenchVersion) {
				fmt.Fprintf(os.Stderr, "parserprobe: %s carries unsupported bench_version %d\n", path, a.BenchVersion)
				os.Exit(2)
			}
			opts.Artifacts = append(opts.Artifacts, a)
			opts.BenchVersion = a.BenchVersion
		}
	} else if !protocol.SupportedBenchVersion(*version) {
		fmt.Fprintf(os.Stderr, "parserprobe: unsupported bench_version %d\n", *version)
		os.Exit(2)
	}

	report, err := parserprobe.Run(opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "parserprobe: %v\n", err)
		os.Exit(1)
	}
	if *out != "" || *asJSON {
		payload, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			fmt.Fprintf(os.Stderr, "parserprobe: %v\n", err)
			os.Exit(1)
		}
		payload = append(payload, '\n')
		if *out != "" {
			if err := os.WriteFile(*out, payload, 0o644); err != nil {
				fmt.Fprintf(os.Stderr, "parserprobe: %v\n", err)
				os.Exit(1)
			}
		}
		if *asJSON {
			_, _ = os.Stdout.Write(payload)
		}
	}
	printSummary(os.Stderr, report)
}

func printSummary(w *os.File, r parserprobe.Report) {
	fmt.Fprintf(w, "bench v%d %s (%s, %d seed(s))\n", r.BenchVersion, r.RunSize, r.Source, len(r.Seeds))
	printVariant(w, "GIH   ", r.GIH)
	if r.Router != nil {
		printVariant(w, fmt.Sprintf("router(%d seeds)", r.RouterSeeds), r.Router)
	}
	fmt.Fprintf(w, "GIH composite per seed: min %.4f max %.4f\n", r.CompositeMin, r.CompositeMax)
	if len(r.Unclassified) > 0 {
		fmt.Fprintf(w, "families with no frame bank: %s\n", strings.Join(r.Unclassified, ", "))
	}
	for _, u := range r.UnmatchedSample {
		fmt.Fprintf(w, "  unmatched: %s\n", u)
	}
}

func printVariant(w *os.File, name string, v *parserprobe.Variant) {
	fmt.Fprintf(w, "%s composite %.4f | memory family-id %.4f answer %.4f mean %.4f | tool family-id %.4f mean %.4f\n",
		name, v.Composite, v.Memory.FamilyIDRate, v.Memory.AnswerRate, v.Memory.Mean, v.Tool.FamilyIDRate, v.Tool.Mean)
	slices := make([]string, 0, len(v.Slices))
	for s := range v.Slices {
		slices = append(slices, s)
	}
	sort.Strings(slices)
	for _, s := range slices {
		st := v.Slices[s]
		fmt.Fprintf(w, "  %-13s cases %5d family-id %.4f answer %.4f mean %.4f\n", s, st.Cases, st.FamilyIDRate, st.AnswerRate, st.Mean)
	}
}

// newestSupportedVersion scans upward from the display version so the default
// tracks the newest contract this module generates, never a hard-coded pin.
func newestSupportedVersion() int {
	newest := protocol.CurrentBenchVersion
	for v := protocol.CurrentBenchVersion; v < protocol.CurrentBenchVersion+64; v++ {
		if protocol.SupportedBenchVersion(v) {
			newest = v
		}
	}
	return newest
}
