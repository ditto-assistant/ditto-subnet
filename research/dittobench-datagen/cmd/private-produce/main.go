// private-produce is an operator-side diagnostic/producer, never a public
// generation endpoint. Output directories must be new and remain private.
package main

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/privatesurface"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	seed := flag.Int64("seed", 0, "explicit nonnegative benchmark seed")
	runSize := flag.String("run-size", "small", "small|medium|full")
	out := flag.String("output", "", "NEW private output directory (required)")
	probe := flag.Int("probe-surfaces", 0, "diagnostic sample only; never emits an accepted artifact (max 32)")
	concurrency := flag.Int("concurrency", 4, "provider concurrency 1..16")
	rewriteModel := flag.String("rewrite-model", "", "explicit OpenRouter model")
	rewriteProvider := flag.String("rewrite-provider", "", "exclusive OpenRouter provider slug")
	validatorModel := flag.String("validator-model", "", "independent semantic validator model")
	validatorProvider := flag.String("validator-provider", "", "exclusive semantic validator provider slug")
	rewriteReasoning := flag.String("rewrite-reasoning", "", "explicit reasoning effort; replaces temperature when set")
	validatorReasoning := flag.String("validator-reasoning", "", "explicit independent-validator reasoning effort")
	saltFile := flag.String("salt-file", "", "private regular 8-byte big-endian reserved entropy file")
	profileOnly := flag.Bool("profile-sha", false, "print profile digest without inference or artifacts")
	maxCost := flag.Float64("max-cost-usd", 0, "required per-invocation allocation from the remaining total spending budget")
	flag.Parse()
	profile := privatesurface.Profile{RewriteModel: *rewriteModel, RewriteProvider: *rewriteProvider, ValidatorModel: *validatorModel, ValidatorProvider: *validatorProvider, RewriteReasoning: *rewriteReasoning, ValidatorReasoning: *validatorReasoning}
	if *profileOnly {
		digest, err := profile.Digest()
		if err != nil {
			return err
		}
		fmt.Println(digest)
		return nil
	}
	if *seed < 0 || *out == "" || *probe < 0 || *probe > 32 {
		return errors.New("private producer: invalid arguments")
	}
	client, err := privatesurface.NewClient(profile, os.Getenv("OPENROUTER_API_KEY"))
	if err != nil {
		return err
	}
	_, ok := gen.ProfileForVersion(*runSize, 13)
	if !ok {
		return errors.New("private producer: invalid run size")
	}
	var salt uint64
	if *saltFile != "" {
		salt, err = readReservedSalt(*saltFile)
		if err != nil {
			return err
		}
	}
	for attempt := 0; salt == 0 && attempt < 2; attempt++ {
		if err := binary.Read(rand.Reader, binary.LittleEndian, &salt); err != nil {
			return errors.New("private producer: entropy unavailable")
		}
	}
	if salt == 0 {
		return errors.New("private producer: invalid entropy")
	}
	base, protectedNoise, err := gen.GeneratePrivateBase(*seed, *runSize, salt)
	if err != nil {
		return errors.New("private producer: base generation failed")
	}
	baseBytes, err := base.Marshal()
	if err != nil {
		return errors.New("private producer: base encoding failed")
	}
	if err := os.Mkdir(*out, 0700); err != nil {
		return errors.New("private producer: output must be a new directory")
	}
	if err := client.EnableBudget(*maxCost, func(snapshot privatesurface.BudgetSnapshot) error {
		return writeBudgetCheckpoint(*out, snapshot)
	}); err != nil {
		return err
	}
	if err := writePrivate(*out, "base.json", baseBytes); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, 2*time.Hour)
	defer cancel()
	if *probe > 0 {
		requests, err := gen.PrivateSurfaceRequests(base, protectedNoise)
		if err != nil {
			return err
		}
		count := min(*probe, len(requests))
		type sample struct {
			Index   int                            `json:"index"`
			Before  string                         `json:"before"`
			After   string                         `json:"after"`
			Error   string                         `json:"error,omitempty"`
			Receipt *privatesurface.SurfaceReceipt `json:"receipt,omitempty"`
		}
		rows := make([]sample, 0, count)
		accepted := 0
		for i := 0; i < count; i++ {
			index := i * len(requests) / count
			after, receipt, err := client.ProbeOne(ctx, requests[index])
			row := sample{Index: index, Before: requests[index].Text, After: after, Receipt: &receipt}
			if err != nil {
				row.Error = err.Error()
			} else {
				accepted++
			}
			rows = append(rows, row)
		}
		raw, _ := json.Marshal(rows)
		if err := writePrivate(*out, "probe.json", raw); err != nil {
			return err
		}
		fmt.Printf("diagnostic only: %d/%d surfaces semantically accepted; %d total surfaces; no artifact approved\n", accepted, count, len(requests))
		if accepted != count {
			return errors.New("private producer: diagnostic sample failed")
		}
		return nil
	}
	var diagnostics []privatesurface.Diagnostic
	dataset, receipt, err := client.ProduceWithDiagnostics(ctx, base, *concurrency, func(d privatesurface.Diagnostic) { diagnostics = append(diagnostics, d) }, protectedNoise)
	trace, _ := json.Marshal(diagnostics)
	if writeErr := writePrivate(*out, "diagnostics.json", trace); writeErr != nil {
		return writeErr
	}
	if err != nil {
		return err
	}
	if err := writePrivate(*out, "dataset.json", dataset); err != nil {
		return err
	}
	if err := writePrivate(*out, "validation.json", receipt); err != nil {
		return err
	}
	fmt.Println("candidate produced privately; NOT qualified, pinned, leased or activated")
	return nil
}

func readReservedSalt(path string) (uint64, error) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() != 8 {
		return 0, errors.New("private producer: invalid entropy file")
	}
	file, err := os.Open(path)
	if err != nil {
		return 0, errors.New("private producer: entropy file unavailable")
	}
	defer file.Close()
	actual, err := file.Stat()
	if err != nil || !os.SameFile(info, actual) {
		return 0, errors.New("private producer: entropy file changed")
	}
	raw, err := io.ReadAll(io.LimitReader(file, 9))
	if err != nil || len(raw) != 8 {
		return 0, errors.New("private producer: invalid entropy length")
	}
	salt := binary.BigEndian.Uint64(raw)
	if salt == 0 {
		return 0, errors.New("private producer: invalid reserved entropy")
	}
	return salt, nil
}

func writeBudgetCheckpoint(out string, snapshot privatesurface.BudgetSnapshot) error {
	raw, err := json.Marshal(snapshot)
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(out, ".spend-*")
	if err != nil {
		return err
	}
	name := file.Name()
	defer os.Remove(name)
	if _, err = file.Write(raw); err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err := os.Rename(name, filepath.Join(out, "spend.json")); err != nil {
		return err
	}
	dir, err := os.Open(out)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func writePrivate(dir, name string, data []byte) error {
	file, err := os.OpenFile(filepath.Join(dir, name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return errors.New("private producer: private output creation failed")
	}
	_, err = file.Write(data)
	if err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err != nil || closeErr != nil {
		return errors.New("private producer: private output write failed")
	}
	return nil
}
