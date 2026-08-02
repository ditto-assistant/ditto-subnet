// Command provider-cert runs the manual OpenRouter provider certification
// matrix for DittoBench's locked harness model. It never prints the API key and
// emits only generalized fixture observations and aggregate usage/cost data.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/providercert"
)

func main() {
	runs := flag.Int("runs", 3, "repeat count per provider/scenario")
	runOffset := flag.Int("run-offset", 0, "number added to run labels when resuming a matrix")
	model := flag.String("model", providercert.DefaultModel, "OpenRouter model slug")
	providers := flag.String("providers", "", "comma-separated provider slugs; empty means every current endpoint")
	output := flag.String("output", "", "write JSON to this file instead of stdout")
	timeout := flag.Duration("timeout", 30*time.Minute, "whole-matrix timeout")
	inventoryOnly := flag.Bool("inventory-only", false, "print current endpoint inventory without making paid requests")
	flag.Parse()

	client := &providercert.Client{APIKey: os.Getenv("OPENROUTER_API_KEY"), MaxAttempts: 3}
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	out := os.Stdout
	if *output != "" {
		file, err := os.Create(*output)
		if err != nil {
			fail(err)
		}
		defer file.Close()
		out = file
	}
	enc := json.NewEncoder(out)
	enc.SetIndent("", "  ")
	if *inventoryOnly {
		inventory, err := client.FetchInventory(ctx, *model)
		if err != nil {
			fail(err)
		}
		if err := enc.Encode(inventory); err != nil {
			fail(err)
		}
		return
	}
	selected := map[string]bool{}
	for _, p := range strings.Split(*providers, ",") {
		if p = strings.TrimSpace(p); p != "" {
			selected[p] = true
		}
	}
	report, err := client.Run(ctx, providercert.RunOptions{Model: *model, Runs: *runs, RunOffset: *runOffset, Providers: selected})
	if err != nil {
		fail(err)
	}
	if err := enc.Encode(report); err != nil {
		fail(err)
	}
}

func fail(err error) {
	_, _ = fmt.Fprintln(os.Stderr, "provider-cert:", err)
	os.Exit(1)
}
