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
	// BenchVersion is the runtime contract the harness is seeded and run
	// under: a round's frozen bench version. Zero uses the pack's declared
	// grading policy. Grading always uses the pack's declared policy.
	BenchVersion int
	// Nonce keys the opaque ID projection. A fresh random nonce is used when
	// empty; tests pin it for stable wire IDs.
	Nonce []byte
	// InferenceBaseURL, when set, returns the case-scoped inference URL the
	// harness should use for one opaque case ID.
	InferenceBaseURL func(opaqueCaseID string) string
	// Tools is the tool catalog advertised on every /run, exactly as a scored
	// memory case advertises the run's catalog. A pack case is a memory
	// question, but a harness built for the scored path expects the catalog.
	Tools []protocol.ToolDefinition
	// ToolEndpoint, when set, returns the observed tool-execution URL for one
	// case. It is called once per case, before its /run, with the pack case ID
	// and the opaque case and user IDs the harness will see, so the caller can
	// register that case's fixture first. An empty result omits tool_endpoint.
	ToolEndpoint func(packCaseID, opaqueCaseID, opaqueUserID string) string
	// ObservedCalls, when set, returns the tool calls the validator watched
	// one case execute through its tool_endpoint. Grading uses them in place
	// of the harness's self-reported trajectory whenever any were observed,
	// exactly as the scored path does, so a harness cannot execute a tool and
	// omit it from its response.
	ObservedCalls func(opaqueCaseID string) []protocol.ObservedToolCall
	// Include, when set, selects the pack cases to run by pack case ID. Every
	// wave is still seeded (an excluded case's evidence may ground another
	// case); excluded cases are never sent to the harness and have no outcome.
	// A dueling slice uses it to drop a case another column already claimed
	// (challenge.SliceExecutionCases).
	Include func(packCaseID string) bool
	// Admit, when set, is asked before each included case starts, in pack
	// order on the dispatching goroutine (never from a worker), so the cases
	// it admits are a prefix of the dispatch order. A case it refuses is not
	// sent to the harness and comes back BudgetExhausted (bench v18 per-run
	// cost budget). Rounds and qualification leave it nil.
	Admit func() bool
}

// CaseOutcome is one graded case: the panel result the scorer submits plus the
// grader's notes for the audit trail.
type CaseOutcome struct {
	challenge.PanelCaseResult
	Notes []string `json:"notes,omitempty"`
	// BudgetExhausted marks a case Options.Admit refused: it never ran.
	BudgetExhausted bool `json:"budget_exhausted,omitempty"`
}

// ErrHarnessUnavailable wraps a failure that prevents grading any case: the
// harness never became healthy or rejected a seeding wave. The scorer reports
// every case of this agent as a fault rather than as misses.
var ErrHarnessUnavailable = errors.New("challenge harness unavailable")

// Execute seeds the pack's waves into the harness and runs every case after its
// wave. It returns one outcome per pack case (per included case when
// Options.Include is set), in pack order.
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
	if opts.BenchVersion == 0 {
		opts.BenchVersion = p.GradingBenchVersion
	}
	proj := newProjection(nonce)
	waves := proj.waves(p.MemoryWaves)
	buckets := runner.StageCasesByWave(len(waves), len(p.Cases), func(i int) int { return p.Cases[i].RunAfterWave })

	outcomes := make([]CaseOutcome, len(p.Cases))
	included := make([]bool, len(p.Cases))
	for i, c := range p.Cases {
		included[i] = opts.Include == nil || opts.Include(c.ID)
	}
	err := runner.RunStagedWaves(ctx, waves, buckets,
		func(ctx context.Context, _ int, wave protocol.SeedRequest) error {
			_, err := runner.SeedForVersion(ctx, harnessURL, wave, opts.BenchVersion)
			return err
		},
		func(ctx context.Context, _ int, bucket []int) error {
			run := make([]int, 0, len(bucket))
			for _, i := range bucket {
				if included[i] {
					run = append(run, i)
				}
			}
			runBoundedAdmitted(len(run), concurrency, opts.Admit, func(j int, admitted bool) {
				i := run[j]
				if !admitted {
					outcomes[i] = CaseOutcome{
						PanelCaseResult: challenge.PanelCaseResult{CaseID: p.Cases[i].ID},
						Notes:           []string{protocol.RunBudgetExhaustedReason},
						BudgetExhausted: true,
					}
					return
				}
				outcomes[i] = runCase(ctx, harnessURL, p.Cases[i], p.GradingCase(i), proj, opts)
			})
			return ctx.Err()
		},
	)
	if err != nil {
		var seedErr *runner.WaveSeedError
		if errors.As(err, &seedErr) {
			return nil, fmt.Errorf("%w: seeding wave %d: %w", ErrHarnessUnavailable, seedErr.Wave, seedErr.Err)
		}
		return nil, err
	}
	if opts.Include == nil {
		return outcomes, nil
	}
	kept := make([]CaseOutcome, 0, len(outcomes))
	for i, o := range outcomes {
		if included[i] {
			kept = append(kept, o)
		}
	}
	return kept, nil
}

func runCase(ctx context.Context, harnessURL string, c challenge.Case, graded protocol.MemoryCase, proj *projection, opts Options) CaseOutcome {
	opaqueID := proj.id("case", c.ID)
	opaqueUser := proj.id("user", c.UserID)
	caseOpts := runner.CaseOptions{UserID: opaqueUser, BenchVersion: opts.BenchVersion}
	if opts.InferenceBaseURL != nil {
		caseOpts.InferenceBaseURL = opts.InferenceBaseURL(opaqueID)
	}
	if opts.ToolEndpoint != nil {
		caseOpts.ToolEndpoint = opts.ToolEndpoint(c.ID, opaqueID, opaqueUser)
	}
	out := CaseOutcome{PanelCaseResult: challenge.PanelCaseResult{CaseID: c.ID}}
	resp, _, err := runner.RunCaseWithTelemetry(ctx, harnessURL, opaqueID, c.Question, opts.Tools, caseOpts)
	if err != nil {
		out.Fault = true
		out.Notes = []string{"no response from harness (error or timeout); excluded as a fault"}
		return out
	}
	if opts.ObservedCalls != nil {
		resp = withObservedCalls(resp, opts.ObservedCalls(opaqueID))
	}
	cs := scorer.GradeMemory(graded, resp)
	out.Score = cs.Score
	out.Notes = cs.Notes
	return out
}

// withObservedCalls grades on the validator-observed trajectory whenever any
// call was observed, the scored path's withObservedTrajectory contract. A
// self-reported call that never reached the endpoint still stands against the
// harness; an executed call the harness omitted is restored.
func withObservedCalls(resp protocol.RunResponse, observed []protocol.ObservedToolCall) protocol.RunResponse {
	if len(observed) > 0 {
		resp.ToolCalls = observed
	}
	return resp
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
	runBoundedAdmitted(n, limit, nil, func(i int, _ bool) { fn(i) })
}

// runBoundedAdmitted asks admit for case i on the dispatching goroutine after
// its slot is acquired and before its worker starts, so admission follows
// case order whatever order the workers finish in. A nil admit admits all.
func runBoundedAdmitted(n, limit int, admit func() bool, fn func(i int, admitted bool)) {
	sem := make(chan struct{}, limit)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		sem <- struct{}{}
		admitted := admit == nil || admit()
		go func(i int, admitted bool) {
			defer wg.Done()
			defer func() { <-sem }()
			fn(i, admitted)
		}(i, admitted)
	}
	wg.Wait()
}
