// Command challenge is the generator competition's local tool: emit the
// reference pack, validate a pack exactly as Platform's scorer will, print its
// identity, and score it against a panel verdict file.
//
// Usage:
//
//	challenge example -seed 42 -hotkey 5Grw... -out pack.json
//	challenge validate pack.json          # exit 1 when invalid
//	challenge validate -json pack.json    # machine-readable report
//	challenge sha pack.json               # the identity to sign and upload
//	challenge score -pack pack.json -panel panel.json [-params params.json]
//
// validate is the same function the trusted scorer runs before any agent sees
// a pack, so a pack that validates locally is accepted on upload unless its
// bytes change.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
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
  example   emit the reference generator's pack for a seed
  validate  check a pack against the v1 contract (exit 1 when invalid)
  sha       print a pack's identity (SHA-256 of its exact bytes)
  score     score a pack against a panel verdict file`)
}

func runExample(args []string) error {
	fs := flag.NewFlagSet("example", flag.ExitOnError)
	seed := fs.Int64("seed", 1, "generator seed")
	hotkey := fs.String("hotkey", "", "miner SS58 hotkey the pack is bound to (required)")
	out := fs.String("out", "", "write the pack here (default: stdout)")
	_ = fs.Parse(args)
	if *hotkey == "" {
		return errors.New("example: -hotkey is required")
	}
	return writeJSON(*out, challenge.ReferencePack(*seed, *hotkey))
}

func runValidate(args []string) error {
	fs := flag.NewFlagSet("validate", flag.ExitOnError)
	asJSON := fs.Bool("json", false, "print the full report as JSON")
	_ = fs.Parse(args)
	if fs.NArg() != 1 {
		return errors.New("validate: expected one pack path")
	}
	report, err := validateFile(fs.Arg(0))
	if err != nil {
		return err
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
	raw, err := readRaw(args[0])
	if err != nil {
		return err
	}
	fmt.Println(challenge.Digest(raw))
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
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return io.ReadAll(io.LimitReader(f, challenge.MaxPackBytes+1))
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
