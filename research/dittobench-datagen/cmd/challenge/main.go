// Command challenge is the generator competition's local tool: rehearse a
// generator exactly as the trusted scorer runs it, package its source for
// upload, and validate or score individual packs.
//
// Usage:
//
//	challenge rehearse -dir ./my-generator -hotkey 5Grw... [-seeds 3] [-cases 16] \
//	    [-sandbox local|docker] [-out-dir packs/]       # exit 1 when invalid
//	challenge pack -dir ./my-generator -out generator.tar.gz
//	challenge inspect generator.tar.gz                   # archive identity and checks
//	challenge rehearse -archive generator.tar.gz -hotkey 5Grw... -sandbox docker
//	challenge validate [-seed <hex> -hotkey 5Grw... -cases 16] pack.json
//	challenge example -seed 42 -hotkey 5Grw... -out pack.json
//	challenge sha FILE
//	challenge score -pack pack.json -panel panel.json [-params params.json]
//
// rehearse runs genrun.Evaluate, the same build/run/validate/determinism path
// the scorer runs on secret seeds. With -sandbox docker it uses the scorer's
// pinned images and limits, so a generator that rehearses cleanly there is
// accepted by the scorer unless it depends on the seeds it happened to see.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "rehearse":
		err = runRehearse(os.Args[2:])
	case "pack":
		err = runPack(os.Args[2:])
	case "inspect":
		err = runInspect(os.Args[2:])
	case "example":
		err = runExample(os.Args[2:])
	case "validate":
		err = runValidate(os.Args[2:])
	case "sha":
		err = runSHA(os.Args[2:])
	case "score":
		err = runScore(os.Args[2:])
	case "-h", "--help", "help":
		usage()
		return
	default:
		usage()
		os.Exit(2)
	}
	var exit exitError
	if errors.As(err, &exit) {
		os.Exit(int(exit))
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "challenge:", err)
		os.Exit(1)
	}
}

type exitError int

func (e exitError) Error() string { return fmt.Sprintf("exit %d", int(e)) }

func usage() {
	fmt.Fprintln(os.Stderr, `usage: challenge <command> [flags]

commands:
  rehearse  build and run a generator as the scorer does (exit 1 when invalid)
  pack      package a generator directory as a deterministic source archive
  inspect   check a source archive and print its identity
  validate  check a pack against the v1 contract (exit 1 when invalid)
  example   emit the reference generator's pack for a seed
  sha       print the SHA-256 of a file's exact bytes
  score     score a pack against a panel verdict file`)
}

func runExample(args []string) error {
	fs := flag.NewFlagSet("example", flag.ExitOnError)
	seed := fs.Int64("seed", 1, "generator seed")
	hotkey := fs.String("hotkey", "", "miner SS58 hotkey the pack is bound to (required)")
	out := fs.String("out", "", "write the pack here (default: stdout)")
	_ = fs.Parse(args)
	if !challenge.ValidHotkey(*hotkey) {
		return errors.New("example: -hotkey must be an SS58 hotkey")
	}
	return writeJSON(*out, challenge.ReferencePack(*seed, *hotkey))
}

func runRehearse(args []string) error {
	fs := flag.NewFlagSet("rehearse", flag.ExitOnError)
	dir := fs.String("dir", "", "generator source directory")
	archive := fs.String("archive", "", "generator source archive (rehearses the exact upload)")
	hotkey := fs.String("hotkey", "", "SS58 hotkey the packs are bound to (required)")
	seeds := fs.Int("seeds", 3, "seeds to run (1..8)")
	firstSeed := fs.Int64("seed", 1, "first rehearsal seed; seeds are derived with challenge.SeedFromInt64")
	cases := fs.Int("cases", 16, "cases per pack (the competition's cases_per_seed)")
	sandbox := fs.String("sandbox", "local", "local (host toolchain, no isolation) or docker (the scorer's sandbox)")
	outDir := fs.String("out-dir", "", "write each seed's pack here as <seed>.json")
	asJSON := fs.Bool("json", false, "print the evaluation as JSON")
	_ = fs.Parse(args)
	if (*dir == "") == (*archive == "") || *hotkey == "" || *seeds < 1 || *seeds > 8 {
		return errors.New("rehearse: exactly one of -dir/-archive, -hotkey, and -seeds 1..8 are required")
	}
	var sb genrun.Sandbox
	switch *sandbox {
	case "local":
		sb = genrun.Local{}
	case "docker":
		sb = genrun.Docker{}
	default:
		return fmt.Errorf("rehearse: unknown sandbox %q", *sandbox)
	}
	// Rehearse the upload, not the checkout: package -dir the way `pack` does,
	// then build from the extracted archive like the scorer.
	var raw []byte
	var err error
	if *archive != "" {
		raw, err = readBounded(*archive, challenge.MaxArchiveBytes, "source archive")
	} else {
		raw, _, err = genrun.PackDir(context.Background(), *dir)
	}
	if err != nil {
		return err
	}
	src, err := os.MkdirTemp("", "challenge-rehearse-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(src)
	if _, err := challenge.ExtractArchive(raw, src); err != nil {
		return err
	}
	// The range ends at firstSeed+seeds-1, which must fit in an int64.
	if *firstSeed < 0 || *firstSeed > math.MaxInt64-int64(*seeds-1) {
		return fmt.Errorf("rehearse: -seed must be in 0..%d for %d seeds", math.MaxInt64-int64(*seeds-1), *seeds)
	}
	plan := genrun.Plan{MinerHotkey: *hotkey, Cases: *cases, CheckDeterminism: true}
	for i := 0; i < *seeds; i++ {
		plan.Seeds = append(plan.Seeds, challenge.SeedFromInt64(*firstSeed+int64(i)))
	}
	ev, err := genrun.Evaluate(context.Background(), sb, src, plan)
	if err != nil {
		return err
	}
	if *outDir != "" {
		if err := os.MkdirAll(*outDir, 0o755); err != nil {
			return err
		}
		for _, s := range ev.Seeds {
			if len(s.Pack) == 0 {
				continue
			}
			if err := os.WriteFile(filepath.Join(*outDir, s.Seed[:16]+".json"), s.Pack, 0o644); err != nil {
				return err
			}
		}
	}
	if *asJSON {
		if err := writeJSON("", ev); err != nil {
			return err
		}
	} else {
		printEvaluation(ev)
	}
	if !ev.Valid() {
		return exitError(1)
	}
	return nil
}

func printEvaluation(ev genrun.Evaluation) {
	if !ev.Build.OK {
		fmt.Printf("build failed (%s sandbox, %dms):\n%s\n", ev.Sandbox, ev.Build.DurationMS, ev.Build.Error)
		return
	}
	fmt.Printf("built (%s sandbox, %dms) binary sha256 %s\n", ev.Sandbox, ev.Build.DurationMS, ev.Build.BinarySHA256)
	for _, s := range ev.Seeds {
		switch {
		case s.Error != "":
			fmt.Printf("seed %s…: %s\n", s.Seed[:12], s.Error)
		case s.Report.Valid:
			fmt.Printf("seed %s…: valid, %d cases, %d pairs, %dms\n", s.Seed[:12], s.Report.CaseCount, s.Report.PairCount, s.DurationMS)
		default:
			fmt.Printf("seed %s…: invalid, %d issue(s)\n", s.Seed[:12], len(s.Report.Issues))
			for _, is := range s.Report.Issues {
				where := "pack"
				if is.CaseID != "" {
					where = "case " + is.CaseID
				}
				fmt.Printf("  %s: [%s] %s\n", where, is.Rule, is.Message)
			}
		}
	}
	if ev.Deterministic != nil {
		if *ev.Deterministic {
			fmt.Println("deterministic: re-running the first seed reproduced identical bytes")
		} else {
			fmt.Println("NOT deterministic: re-running the first seed produced different output")
		}
	}
	if ev.Valid() {
		fmt.Println("valid: the scorer would accept this generator on these seeds")
	} else {
		fmt.Println("invalid: the scorer would reject this generator")
	}
}

func runPack(args []string) error {
	fs := flag.NewFlagSet("pack", flag.ExitOnError)
	dir := fs.String("dir", "", "generator source directory (required)")
	out := fs.String("out", "generator.tar.gz", "archive path")
	_ = fs.Parse(args)
	if *dir == "" {
		return errors.New("pack: -dir is required")
	}
	raw, info, err := genrun.PackDir(context.Background(), *dir)
	if err != nil {
		return fmt.Errorf("pack: %w", err)
	}
	if err := os.WriteFile(*out, raw, 0o644); err != nil {
		return err
	}
	fmt.Printf("wrote %s: %d files, %d bytes source, archive sha256 %s, tree sha256 %s\n", *out, info.Files, info.Bytes, info.SHA256, info.TreeSHA256)
	return nil
}

func runInspect(args []string) error {
	if len(args) != 1 {
		return errors.New("inspect: expected one archive path")
	}
	raw, err := readBounded(args[0], challenge.MaxArchiveBytes, "source archive")
	if err != nil {
		return err
	}
	info, _, err := challenge.InspectArchive(raw)
	if err != nil {
		return err
	}
	return writeJSON("", info)
}

func runValidate(args []string) error {
	fs := flag.NewFlagSet("validate", flag.ExitOnError)
	asJSON := fs.Bool("json", false, "print the full report as JSON")
	seed := fs.String("seed", "", "also check the pack is bound to this request seed")
	hotkey := fs.String("hotkey", "", "request hotkey for the binding check")
	cases := fs.Int("cases", 0, "request case count for the binding check")
	_ = fs.Parse(args)
	if fs.NArg() != 1 {
		return errors.New("validate: expected one pack path")
	}
	report, err := validateFile(fs.Arg(0))
	if err != nil {
		return err
	}
	if *seed != "" {
		raw, err := readRaw(fs.Arg(0))
		if err != nil {
			return err
		}
		report = challenge.ValidateGenerated(raw, challenge.NewGenerateRequest(*seed, *hotkey, *cases))
	}
	if *asJSON {
		if err := writeJSON("", report); err != nil {
			return err
		}
	} else {
		for _, is := range report.Issues {
			where := "pack"
			if is.CaseID != "" {
				where = "case " + is.CaseID
			}
			fmt.Printf("%s: [%s] %s\n", where, is.Rule, is.Message)
		}
		if report.Valid {
			fmt.Printf("valid: %d cases, %d pairs, sha256 %s\n", report.CaseCount, report.PairCount, report.PackSHA256)
		} else {
			fmt.Printf("invalid: %d issue(s)\n", len(report.Issues))
		}
	}
	if !report.Valid {
		return exitError(1)
	}
	return nil
}

func validateFile(path string) (challenge.Report, error) {
	raw, err := readRaw(path)
	if err != nil {
		return challenge.Report{}, err
	}
	return challenge.ValidateBytes(raw), nil
}

func runSHA(args []string) error {
	if len(args) != 1 {
		return errors.New("sha: expected one pack path")
	}
	// The exact file bytes, whatever their size: a digest of a capped prefix
	// would not identify the file.
	f, err := os.Open(args[0])
	if err != nil {
		return err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return err
	}
	fmt.Println(hex.EncodeToString(h.Sum(nil)))
	return nil
}

func runScore(args []string) error {
	fs := flag.NewFlagSet("score", flag.ExitOnError)
	packPath := fs.String("pack", "", "pack path (required)")
	panelPath := fs.String("panel", "", "panel verdict file: a challenge.ScoreInput JSON object (required)")
	paramsPath := fs.String("params", "", "score params JSON (default: shipped v1 params)")
	_ = fs.Parse(args)
	if *packPath == "" || *panelPath == "" {
		return errors.New("score: -pack and -panel are required")
	}
	p, err := readPack(*packPath)
	if err != nil {
		return err
	}
	if r := challenge.Validate(p); !r.Valid {
		return fmt.Errorf("score: pack is invalid (%d issues); run validate", len(r.Issues))
	}
	var in challenge.ScoreInput
	if err := readJSON(*panelPath, &in); err != nil {
		return err
	}
	params := challenge.DefaultScoreParams()
	if *paramsPath != "" {
		if err := readJSON(*paramsPath, &params); err != nil {
			return err
		}
	}
	score, err := challenge.Score(p, in, params)
	if err != nil {
		return err
	}
	return writeJSON("", score)
}

func readRaw(path string) ([]byte, error) {
	return readBounded(path, challenge.MaxPackBytes, "pack")
}

// readBounded reads a file and refuses one larger than limit instead of
// silently truncating it.
func readBounded(path string, limit int64, what string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) > limit {
		return nil, fmt.Errorf("%s %s exceeds %d bytes", what, path, limit)
	}
	return raw, nil
}

func readPack(path string) (challenge.Pack, error) {
	raw, err := readRaw(path)
	if err != nil {
		return challenge.Pack{}, err
	}
	return challenge.Decode(raw)
}

func readJSON(path string, v any) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, v)
}

func writeJSON(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	if path == "" {
		_, err = os.Stdout.Write(b)
		return err
	}
	return os.WriteFile(path, b, 0o644)
}
