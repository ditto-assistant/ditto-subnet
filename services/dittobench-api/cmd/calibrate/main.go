// Command calibrate measures DittoBench *test difficulty* variance.
//
// It runs K evaluations of the SAME (fixed) harness against the API, each on a
// freshly generated dataset (rotating seed), and reports the mean and standard
// deviation of the scores. With a deterministic harness (cmd/refharness) the
// only thing that changes between runs is the dataset, so the spread of scores
// IS the spread of dataset difficulty — the number we want tight.
//
// The headline metric is the stddev of `tool_mean` (the deterministic
// tool-accuracy half, the clean difficulty signal).
// `--max-stddev` (default 0.03) gates pass/fail.
//
// A `--repeat-seed` noise-floor pass runs R evaluations at one PINNED seed; with
// a deterministic harness that stddev should be ~0, confirming the measurement
// isolates dataset difficulty rather than harness noise.
//
// CALIBRATION TRUST ASSUMPTION: the --max-stddev gate is only as honest as the
// harness it runs against. Pointed at the DETERMINISTIC refharness (or any
// zero-variance parser) it measures dataset-difficulty spread ALONE and reports
// a noise floor of ~0 — it says nothing about the run-to-run variance a real
// locked-model reasoning harness carries. Tuning the gate (or the KOTH
// margin/score_tol it feeds) against that number will certify a deterministic
// parser as a stable champion. To gate honestly, run this against a real
// locked-model harness over many fresh seeds and set --max-stddev from the
// MEASURED champion-region σ, not the refharness floor. See
// docs/calibration-trust.md.
//
//	calibrate --runs 100 --harness http://localhost:9000   # direct (tool-only)
//	calibrate --runs 100 --run-size full --harness ...      # full pipeline
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"time"
)

type sample struct {
	seed       int64
	composite  float64
	toolMean   float64
	memoryMean float64
}

func main() {
	api := flag.String("api", "http://localhost:8000", "dittobench-api base URL")
	harness := flag.String("harness", "http://localhost:9000", "fixed harness URL (the 'same model')")
	runs := flag.Int("runs", 100, "number of runs (rotating seed each)")
	n := flag.Int("n", 30, "tool cases per run (direct path only)")
	runSize := flag.String("run-size", "", "small|medium|full for the full pipeline; empty = direct path")
	repeatSeed := flag.Int64("repeat-seed", 0, "if non-zero, also run --repeats evals at this pinned seed (noise floor)")
	repeats := flag.Int("repeats", 10, "repeats for the --repeat-seed noise-floor pass")
	// The gate is only meaningful against a REAL locked-model harness: against the
	// deterministic refharness this measures dataset difficulty alone (harness σ
	// = 0). Calibrate the threshold from a real harness's measured champion-region
	// σ, not the refharness floor. See docs/calibration-trust.md.
	maxStddev := flag.Float64("max-stddev", 0.03, "pass/fail gate on stddev(tool_mean); calibrate against a REAL locked-model harness, not the deterministic refharness (see docs/calibration-trust.md)")
	flag.Parse()

	c := &client{api: *api, harness: *harness, n: *n, runSize: *runSize}
	ctx := context.Background()

	log.Printf("calibrate: %d runs against %s (run_size=%q n=%d)", *runs, *harness, *runSize, *n)
	between := collect(ctx, c, *runs, 0)
	report("between-seed (test difficulty)", between)

	tm := field(between, func(s sample) float64 { return s.toolMean })
	sd := stddev(tm)
	fmt.Printf("\nheadline: stddev(tool_mean) = %.4f  (gate %.4f)\n", sd, *maxStddev)

	if *repeatSeed != 0 {
		floor := collect(ctx, c, *repeats, *repeatSeed)
		report(fmt.Sprintf("within-seed noise floor (seed %d)", *repeatSeed), floor)
		nf := stddev(field(floor, func(s sample) float64 { return s.toolMean }))
		fmt.Printf("noise floor: stddev(tool_mean) = %.4f (should be ~0 for a deterministic harness)\n", nf)
	}

	if sd > *maxStddev {
		fmt.Printf("\nFAIL: difficulty stddev %.4f exceeds %.4f — tighten with stratified category\n"+
			"sampling and/or a larger n.\n", sd, *maxStddev)
		os.Exit(1)
	}
	fmt.Printf("\nPASS: difficulty stddev %.4f within %.4f.\n", sd, *maxStddev)
}

// collect runs `count` evals. seed==0 → fresh rotating seed each; otherwise the
// seed is pinned (noise-floor pass).
func collect(ctx context.Context, c *client, count int, seed int64) []sample {
	out := make([]sample, 0, count)
	for i := 0; i < count; i++ {
		s, err := c.run(ctx, seed)
		if err != nil {
			log.Fatalf("run %d/%d failed: %v", i+1, count, err)
		}
		out = append(out, s)
		if (i+1)%10 == 0 || i+1 == count {
			log.Printf("  %d/%d runs", i+1, count)
		}
	}
	return out
}

func report(label string, ss []sample) {
	comp := field(ss, func(s sample) float64 { return s.composite })
	tm := field(ss, func(s sample) float64 { return s.toolMean })
	mm := field(ss, func(s sample) float64 { return s.memoryMean })
	fmt.Printf("\n%s — %d runs\n", label, len(ss))
	fmt.Printf("  composite   mean=%.4f stddev=%.4f min=%.4f max=%.4f\n",
		mean(comp), stddev(comp), minf(comp), maxf(comp))
	fmt.Printf("  tool_mean   mean=%.4f stddev=%.4f min=%.4f max=%.4f\n",
		mean(tm), stddev(tm), minf(tm), maxf(tm))
	if mean(mm) > 0 || stddev(mm) > 0 {
		fmt.Printf("  memory_mean mean=%.4f stddev=%.4f min=%.4f max=%.4f\n",
			mean(mm), stddev(mm), minf(mm), maxf(mm))
	}
}

// ---- API client ----

type client struct {
	api, harness string
	n            int
	runSize      string
}

type submitReq struct {
	HarnessURL string `json:"harness_url"`
	N          int    `json:"n,omitempty"`
	RunSize    string `json:"run_size,omitempty"`
	Seed       int64  `json:"seed,omitempty"`
}

func (c *client) run(ctx context.Context, seed int64) (sample, error) {
	body := submitReq{HarnessURL: c.harness, Seed: seed}
	if c.runSize != "" {
		body.RunSize = c.runSize
	} else {
		body.N = c.n
	}
	raw, status, err := c.post(ctx, "/v1/submit", body)
	if err != nil {
		return sample{}, err
	}

	if c.runSize == "" { // direct path: synchronous 200 with the summary
		if status != http.StatusOK {
			return sample{}, fmt.Errorf("submit returned %d: %s", status, truncate(raw))
		}
		var r struct {
			Composite float64 `json:"composite"`
			ToolMean  float64 `json:"tool_mean"`
			Seed      int64   `json:"seed"`
		}
		if err := json.Unmarshal(raw, &r); err != nil {
			return sample{}, fmt.Errorf("decode direct response: %w", err)
		}
		return sample{seed: r.Seed, composite: r.Composite, toolMean: r.ToolMean}, nil
	}

	// run_size: 202 + run_id, then poll.
	if status != http.StatusAccepted && status != http.StatusOK {
		return sample{}, fmt.Errorf("submit returned %d: %s", status, truncate(raw))
	}
	var acc struct {
		RunID string `json:"run_id"`
	}
	if err := json.Unmarshal(raw, &acc); err != nil || acc.RunID == "" {
		return sample{}, fmt.Errorf("decode run_id: %w (%s)", err, truncate(raw))
	}
	return c.poll(ctx, acc.RunID)
}

func (c *client) poll(ctx context.Context, runID string) (sample, error) {
	deadline := time.Now().Add(30 * time.Minute)
	for {
		raw, _, err := c.get(ctx, "/v1/runs/"+runID)
		if err != nil {
			return sample{}, err
		}
		var job struct {
			Status string `json:"status"`
			Report struct {
				Composite  float64 `json:"composite"`
				ToolMean   float64 `json:"tool_mean"`
				MemoryMean float64 `json:"memory_mean"`
				Seed       int64   `json:"seed"`
			} `json:"report"`
			Error string `json:"error"`
		}
		if err := json.Unmarshal(raw, &job); err != nil {
			return sample{}, fmt.Errorf("decode job: %w", err)
		}
		switch job.Status {
		case "done":
			return sample{
				seed:       job.Report.Seed,
				composite:  job.Report.Composite,
				toolMean:   job.Report.ToolMean,
				memoryMean: job.Report.MemoryMean,
			}, nil
		case "failed":
			return sample{}, fmt.Errorf("run %s failed: %s", runID, job.Error)
		}
		if time.Now().After(deadline) {
			return sample{}, fmt.Errorf("run %s timed out", runID)
		}
		time.Sleep(2 * time.Second)
	}
}

func (c *client) post(ctx context.Context, path string, body any) ([]byte, int, error) {
	b, _ := json.Marshal(body)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, c.api+path, bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	return do(req)
}

func (c *client) get(ctx context.Context, path string) ([]byte, int, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.api+path, nil)
	return do(req)
}

func do(req *http.Request) ([]byte, int, error) {
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	return b, resp.StatusCode, err
}

func truncate(b []byte) string {
	if len(b) > 200 {
		return string(b[:200])
	}
	return string(b)
}

// ---- stats ----

func field(ss []sample, f func(sample) float64) []float64 {
	out := make([]float64, len(ss))
	for i, s := range ss {
		out[i] = f(s)
	}
	return out
}

func mean(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	var sum float64
	for _, x := range xs {
		sum += x
	}
	return sum / float64(len(xs))
}

// stddev is the sample standard deviation (n-1); 0 for fewer than two points.
func stddev(xs []float64) float64 {
	if len(xs) < 2 {
		return 0
	}
	m := mean(xs)
	var ss float64
	for _, x := range xs {
		d := x - m
		ss += d * d
	}
	return math.Sqrt(ss / float64(len(xs)-1))
}

func minf(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	m := xs[0]
	for _, x := range xs {
		if x < m {
			m = x
		}
	}
	return m
}

func maxf(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	m := xs[0]
	for _, x := range xs {
		if x > m {
			m = x
		}
	}
	return m
}
