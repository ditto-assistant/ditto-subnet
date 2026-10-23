// Package challengerun executes a generator-competition challenge pack against
// one ready agent harness and grades it with the production memory grader.
//
// It is the per-agent half of a panel run: the scorer calls Execute once per
// panel member, each against a fresh harness, and hands the collected results
// to challenge.ScoreCases. Execution reuses the scored path's primitives
// (runner.SeedForVersion, the wave barrier, runner.RunCaseWithTelemetry, and
// scorer.GradeMemory), so a pack case reaches an agent and is graded exactly
// like a benchmark memory case at the pack's grading bench version.
//
// Pack identities never cross the harness wire. Case, pair, session, and user
// IDs are replaced with per-run opaque IDs keyed by a random nonce, so a
// harness cannot recognize a generator's ID scheme or correlate runs of the
// same pack. Opaque user IDs also scope each run to its own memory graph, so a
// harness reused across packs never answers from another pack's haystack.
package challengerun

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// DefaultConcurrency bounds concurrent cases within one wave. Cases inside a
// wave are independent once the wave is seeded, matching the scored path.
const DefaultConcurrency = 4

// Options tune one execution. The zero value is valid.
type Options struct {
	// Concurrency bounds in-flight cases per wave (default DefaultConcurrency).
	Concurrency int
	// Nonce keys the opaque ID projection. A fresh random nonce is used when
	// empty; tests pin it for stable wire IDs.
	Nonce []byte
	// InferenceBaseURL, when set, returns the case-scoped inference URL the
	// harness should use for one opaque case ID.
	InferenceBaseURL func(opaqueCaseID string) string
}

// CaseOutcome is one graded case: the panel result the scorer submits plus the
// grader's notes for the audit trail.
type CaseOutcome struct {
	challenge.PanelCaseResult
	Notes []string `json:"notes,omitempty"`
}

// ErrHarnessUnavailable wraps a failure that prevents grading any case: the
// harness never became healthy or rejected a seeding wave. The scorer reports
// every case of this agent as a fault rather than as misses.
var ErrHarnessUnavailable = errors.New("challenge harness unavailable")

// Execute seeds the pack's waves into the harness and runs every case after its
// wave. It returns one outcome per pack case, in pack order.
//
// A case whose /run fails (transport error or timeout) is a Fault, not a
// miss: a scorer-side blip must never be able to manufacture "the champion
// failed this case". A seeding failure aborts with ErrHarnessUnavailable.
func Execute(ctx context.Context, harnessURL string, p challenge.Pack, opts Options) ([]CaseOutcome, error) {
	if r := challenge.Validate(p); !r.Valid {
		return nil, fmt.Errorf("challenge pack is invalid (%d issues)", len(r.Issues))
	}
	nonce := opts.Nonce
	if len(nonce) == 0 {
		nonce = make([]byte, 32)
		if _, err := rand.Read(nonce); err != nil {
			return nil, fmt.Errorf("challenge run nonce: %w", err)
		}
	}
	concurrency := opts.Concurrency
	if concurrency <= 0 {
		concurrency = DefaultConcurrency
	}
	proj := newProjection(nonce)
	waves := proj.waves(p.MemoryWaves)
	buckets := runner.StageCasesByWave(len(waves), len(p.Cases), func(i int) int { return p.Cases[i].RunAfterWave })

	outcomes := make([]CaseOutcome, len(p.Cases))
	err := runner.RunStagedWaves(ctx, waves, buckets,
		func(ctx context.Context, _ int, wave protocol.SeedRequest) error {
			_, err := runner.SeedForVersion(ctx, harnessURL, wave, challenge.GradingBenchVersion)
			return err
		},
		func(ctx context.Context, _ int, bucket []int) error {
			runBounded(len(bucket), concurrency, func(j int) {
				i := bucket[j]
				outcomes[i] = runCase(ctx, harnessURL, p.Cases[i], proj, opts)
			})
			return ctx.Err()
		},
	)
	if err != nil {
		var seedErr *runner.WaveSeedError
		if errors.As(err, &seedErr) {
			return nil, fmt.Errorf("%w: seeding wave %d: %v", ErrHarnessUnavailable, seedErr.Wave, seedErr.Err)
		}
		return nil, err
	}
	return outcomes, nil
}

func runCase(ctx context.Context, harnessURL string, c challenge.Case, proj *projection, opts Options) CaseOutcome {
	opaqueID := proj.id("case", c.ID)
	caseOpts := runner.CaseOptions{UserID: proj.id("user", c.UserID), BenchVersion: challenge.GradingBenchVersion}
	if opts.InferenceBaseURL != nil {
		caseOpts.InferenceBaseURL = opts.InferenceBaseURL(opaqueID)
	}
	out := CaseOutcome{PanelCaseResult: challenge.PanelCaseResult{CaseID: c.ID}}
	resp, _, err := runner.RunCaseWithTelemetry(ctx, harnessURL, opaqueID, c.Question, nil, caseOpts)
	if err != nil {
		out.Fault = true
		out.Notes = []string{"no response from harness (error or timeout); excluded as a fault"}
		return out
	}
	cs := scorer.GradeMemory(c.GradingCase(), resp)
	out.Score = cs.Score
	out.Notes = cs.Notes
	return out
}

// projection maps pack identities to per-run opaque identities.
type projection struct {
	key []byte
}

func newProjection(nonce []byte) *projection {
	return &projection{key: append([]byte(nil), nonce...)}
}

func (p *projection) id(kind, value string) string {
	mac := hmac.New(sha256.New, p.key)
	mac.Write([]byte(kind))
	mac.Write([]byte{0})
	mac.Write([]byte(value))
	return kind[:1] + "-" + hex.EncodeToString(mac.Sum(nil))[:24]
}

func (p *projection) waves(in []protocol.SeedRequest) []protocol.SeedRequest {
	out := make([]protocol.SeedRequest, len(in))
	for i, w := range in {
		w2 := protocol.SeedRequest{UserID: p.id("user", w.UserID), Wave: w.Wave}
		w2.Pairs = make([]protocol.MemoryPair, len(w.Pairs))
		for j, pr := range w.Pairs {
			pr.PairID = p.id("pair", pr.PairID)
			pr.SessionID = p.id("session", pr.SessionID)
			w2.Pairs[j] = pr
		}
		out[i] = w2
	}
	return out
}

func runBounded(n, limit int, fn func(i int)) {
	sem := make(chan struct{}, limit)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		sem <- struct{}{}
		go func(i int) {
			defer wg.Done()
			defer func() { <-sem }()
			fn(i)
		}(i)
	}
	wg.Wait()
}
