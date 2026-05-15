// Command validator is the production-shaped DittoBench validator binary.
//
// It loads the fixture corpus, partitions it into public/private/canary
// buckets via the antigaming helpers, drives one or more miner harness
// images over the stdio protocol, scores each response with the
// canonical Go scorer, applies the memorisation discount, and emits a
// report. When given chain credentials it also commits the resulting
// weight vector via the chain.Client.
//
// The binary is intentionally hermetic in its happy path: no MCP, no
// global state, no log surfaces other than stderr. Reports are JSON on
// disk; weights leave the binary only through chain.Client.PutWeights.
//
// Usage example:
//
//	ditto-validator \
//	  --fixtures-root ./ditto/bench/fixtures \
//	  --miner-commitments ./out/miners.json \
//	  --secret "validator-tempo-1" \
//	  --mechanism ditto_core \
//	  --report-dir ./out/reports
//
// See `--help` for the full flag set.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"syscall"
	"time"
)

func main() {
	cfg, err := parseFlags(os.Args[1:])
	if err != nil {
		fmt.Fprintf(os.Stderr, "validator: %v\n", err)
		os.Exit(2)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	if err := run(ctx, cfg, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "validator: %v\n", err)
		os.Exit(1)
	}
}

// parseFlags builds a Config from CLI args. Exported for testing.
func parseFlags(args []string) (Config, error) {
	fs := flag.NewFlagSet("ditto-validator", flag.ContinueOnError)
	cfg := Config{
		Mechanism:       "all",
		Visibility:      "all",
		BudgetLatencyMs: 4000,
		DeadlineMs:      8000,
		PrivateFrac:     0.25,
		CanaryFrac:      0.15,
	}

	fs.StringVar(&cfg.FixturesRoot, "fixtures-root", "", "Path to the ditto-subnet fixtures directory (REQUIRED).")
	fs.StringVar(&cfg.MinerCommitmentsPath, "miner-commitments", "", "Path to a JSON file mapping hotkey -> {image, env, network}. Required unless --self-test.")
	fs.StringVar(&cfg.Secret, "secret", "", "Validator-controlled secret seed for partition/paraphrase. REQUIRED.")
	fs.StringVar(&cfg.Mechanism, "mechanism", cfg.Mechanism, "Mechanism to score: ditto_core | ditto_retrieval | all.")
	fs.StringVar(&cfg.Visibility, "visibility", cfg.Visibility, "Visibility bucket to score: public | private | canary | all.")
	fs.IntVar(&cfg.Sample, "sample", 0, "Random sample size per mechanism (0 = all).")
	fs.Int64Var(&cfg.DeadlineMs, "deadline-ms", cfg.DeadlineMs, "Per-case wall-clock budget in milliseconds.")
	fs.Int64Var(&cfg.BudgetLatencyMs, "budget-latency-ms", cfg.BudgetLatencyMs, "Latency-score budget (decays linearly to 0 at 5x).")
	fs.Float64Var(&cfg.PrivateFrac, "private-frac", cfg.PrivateFrac, "Private bucket fraction for partition_fixture.")
	fs.Float64Var(&cfg.CanaryFrac, "canary-frac", cfg.CanaryFrac, "Canary bucket fraction for partition_fixture.")
	fs.StringVar(&cfg.ReportDir, "report-dir", "", "Directory for per-miner JSON reports (empty = no reports written).")
	fs.IntVar(&cfg.Netuid, "netuid", 118, "Subtensor netuid to commit weights to.")

	fs.StringVar(&cfg.PylonURL, "pylon-url", "", "Pylon HTTP URL; when empty the validator scores+aggregates but skips put_weights.")
	fs.StringVar(&cfg.PylonIdentityName, "pylon-identity-name", "", "Pylon identity name.")
	fs.StringVar(&cfg.PylonIdentityToken, "pylon-identity-token", "", "Pylon identity token.")

	fs.BoolVar(&cfg.SelfTest, "self-test", false, "Run the in-process echo-harness pipeline (no docker, no chain).")
	fs.BoolVar(&cfg.DryRun, "dry-run", false, "Score and aggregate but never call PutWeights even when Pylon flags are set.")

	if err := fs.Parse(args); err != nil {
		return Config{}, err
	}
	if cfg.FixturesRoot == "" {
		return Config{}, fmt.Errorf("--fixtures-root is required")
	}
	if cfg.Secret == "" {
		return Config{}, fmt.Errorf("--secret is required")
	}
	if cfg.MinerCommitmentsPath == "" && !cfg.SelfTest {
		return Config{}, fmt.Errorf("--miner-commitments is required unless --self-test is set")
	}
	if cfg.PrivateFrac < 0 || cfg.PrivateFrac > 0.5 {
		return Config{}, fmt.Errorf("--private-frac must be in [0, 0.5], got %v", cfg.PrivateFrac)
	}
	if cfg.CanaryFrac < 0 || cfg.CanaryFrac > 0.5 {
		return Config{}, fmt.Errorf("--canary-frac must be in [0, 0.5], got %v", cfg.CanaryFrac)
	}
	return cfg, nil
}

// run is the side-effect entry point; main() turns its error into an exit code.
func run(ctx context.Context, cfg Config, logw *os.File) error {
	logf := func(format string, args ...any) {
		fmt.Fprintf(logw, "validator: "+format+"\n", args...)
	}

	commitments, err := loadCommitments(cfg)
	if err != nil {
		return err
	}
	if len(commitments) == 0 {
		return fmt.Errorf("no miner commitments loaded; nothing to do")
	}

	core, retrieval, err := loadFixtures(cfg.FixturesRoot)
	if err != nil {
		return fmt.Errorf("load fixtures: %w", err)
	}
	logf("loaded %d core cases, %d retrieval cases", len(core), len(retrieval))

	plan := planPartitions(core, retrieval, cfg)

	hkByChallenge := map[string]string{}
	allScores := make([]scoreWithMech, 0, len(commitments)*32)

	if cfg.ReportDir != "" {
		if err := os.MkdirAll(cfg.ReportDir, 0o755); err != nil {
			return fmt.Errorf("create report dir: %w", err)
		}
	}

	hotkeys := make([]string, 0, len(commitments))
	for hk := range commitments {
		hotkeys = append(hotkeys, hk)
	}
	sort.Strings(hotkeys)

	for _, hk := range hotkeys {
		commit := commitments[hk]
		logf("scoring %s (%s)", hk, commit.Image)
		minerScores, perChallenge, err := scoreMiner(ctx, hk, commit, plan, cfg, logf)
		if err != nil {
			logf("miner %s aborted: %v", hk, err)
			continue
		}
		for cid, mhk := range perChallenge {
			hkByChallenge[cid] = mhk
		}
		allScores = append(allScores, minerScores...)
		if cfg.ReportDir != "" {
			path := filepath.Join(cfg.ReportDir, "miner-"+sanitize(hk)+".json")
			if err := writeMinerReport(path, hk, commit, minerScores); err != nil {
				logf("write report for %s failed: %v", hk, err)
			}
		}
	}

	// Aggregate per mechanism (validators commit weights independently
	// per mechanism so a strong Core miner is not pulled down by weak
	// Retrieval performance and vice versa).
	if err := finaliseAndCommit(ctx, cfg, allScores, hkByChallenge, logf); err != nil {
		return err
	}
	_ = time.Now // keep the time import live; future report adds graded-at stamps
	return nil
}
