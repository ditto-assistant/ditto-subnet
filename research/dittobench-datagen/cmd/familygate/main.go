// Command familygate prints the SN-351 per-family admission table for the
// bench v14 qualification report.
//
// The input is a JSON array of familygate.CaseRow: one judged case (or a
// counted column aggregate) per row, labelled by family. Rows come from a
// Backroom generator round (get_generator_round per-case verdicts), a private
// operator column, or an offline panel run of v14 datagen. The command never
// fetches anything itself.
//
// Usage:
//
//	familygate -in rows.json                  # markdown table
//	familygate -in rows.json -json            # machine-readable rows
//	familygate -in rows.json -require a,b     # exit 1 unless a and b are admitted
//	familygate < rows.json
//
// Thresholds default to familygate.DefaultParams and can be overridden for a
// what-if report; the qualification record must state the values it used.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/familygate"
)

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "familygate:", err)
		os.Exit(1)
	}
}

func run(args []string, stdin io.Reader, stdout io.Writer) error {
	defaults := familygate.DefaultParams()
	fs := flag.NewFlagSet("familygate", flag.ContinueOnError)
	in := fs.String("in", "", "case rows JSON file (default stdin)")
	asJSON := fs.Bool("json", false, "emit JSON rows instead of markdown")
	require := fs.String("require", "", "comma-separated families that must be admitted; exit 1 otherwise")
	p := defaults
	fs.IntVar(&p.MinRounds, "min-rounds", defaults.MinRounds, "fewest distinct rounds per family")
	fs.IntVar(&p.MinSolverOwners, "min-solver-owners", defaults.MinSolverOwners, "distinct owners that must solve a case")
	fs.Float64Var(&p.SolveThreshold, "solve-threshold", defaults.SolveThreshold, "per-case score that counts as solved")
	fs.Float64Var(&p.MaxTrivialRate, "max-trivial-rate", defaults.MaxTrivialRate, "highest admitted trivial rate")
	fs.Float64Var(&p.MaxUnsolvedRate, "max-unsolved-rate", defaults.MaxUnsolvedRate, "highest admitted unsolved rate")
	if err := fs.Parse(args); err != nil {
		return err
	}
	var src io.Reader = stdin
	if *in != "" {
		f, err := os.Open(*in)
		if err != nil {
			return err
		}
		defer f.Close()
		src = f
	}
	dec := json.NewDecoder(src)
	dec.DisallowUnknownFields()
	var rows []familygate.CaseRow
	if err := dec.Decode(&rows); err != nil {
		return fmt.Errorf("decode case rows: %w", err)
	}
	report, err := familygate.Evaluate(rows, p)
	if err != nil {
		return err
	}
	if *asJSON {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(report); err != nil {
			return err
		}
	} else {
		fmt.Fprint(stdout, familygate.Markdown(report, p))
	}
	if *require == "" {
		return nil
	}
	verdicts := map[string]string{}
	for _, r := range report {
		verdicts[r.Family] = r.Verdict
	}
	var held []string
	for _, family := range strings.Split(*require, ",") {
		family = strings.TrimSpace(family)
		if family == "" {
			continue
		}
		switch v, ok := verdicts[family]; {
		case !ok:
			held = append(held, family+" (no rows)")
		case v != familygate.Admit:
			held = append(held, family+" ("+v+")")
		}
	}
	if len(held) > 0 {
		return fmt.Errorf("not admitted: %s", strings.Join(held, ", "))
	}
	return nil
}
