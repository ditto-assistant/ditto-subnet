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
	flag.Parse()
	if *seed < 0 || *out == "" || *probe < 0 || *probe > 32 {
		return errors.New("private producer: invalid arguments")
	}
	profile := privatesurface.Profile{RewriteModel: *rewriteModel, RewriteProvider: *rewriteProvider, ValidatorModel: *validatorModel, ValidatorProvider: *validatorProvider}
	client, err := privatesurface.NewClient(profile, os.Getenv("OPENROUTER_API_KEY"))
	if err != nil {
		return err
	}
	p, ok := gen.ProfileForVersion(*runSize, 13)
	if !ok {
		return errors.New("private producer: invalid run size")
	}
	var salt uint64
	for salt == 0 {
		if err := binary.Read(rand.Reader, binary.LittleEndian, &salt); err != nil {
			return errors.New("private producer: entropy unavailable")
		}
	}
	base, err := gen.GenerateDatasetWithSurface(*seed, p, 13, gen.SurfaceOptions{Salt: salt})
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
	if err := writePrivate(*out, "base.json", baseBytes); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, 2*time.Hour)
	defer cancel()
	if *probe > 0 {
		requests, err := gen.PrivateSurfaceRequests(base)
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
	dataset, receipt, err := client.ProduceWithDiagnostics(ctx, base, *concurrency, func(d privatesurface.Diagnostic) { diagnostics = append(diagnostics, d) })
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
