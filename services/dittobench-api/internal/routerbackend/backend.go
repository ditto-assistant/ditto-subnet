// Package routerbackend is the SN118 router track's compute-destination seam:
// one RouterBackend interface with swappable implementations, so the (heavy)
// router scoring is never locked to a single compute destination. It sits above
// both internal/routerharness (the harness adapters + the opt-in inclusion
// probe) and internal/routerscore (the ledger fold), tying them together without
// an import cycle.
//
// The opt-in ("yes-and") inclusion gate lives once here in Dispatcher.Run, ahead
// of any backend dispatch, so every implementation — in-process LocalBackend
// today, an offloaded/remote backend later — shares identical backwards-
// compatible semantics: a submission that advertises no router project is a
// benign skip and its memory scoring is untouched. Everything here is SHADOW and
// never weight-eligible.
package routerbackend

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/routerharness"
	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
)

// ErrNotIncluded is returned by Dispatcher.Run when the submission advertised no
// router project (the /router/health probe came back StatusUnsupported). It is
// benign: the caller skips the router run and scores the submission on the
// memory contract exactly as before. Callers classify it with errors.Is.
var ErrNotIncluded = errors.New("routerbackend: submission advertised no router project")

// ErrOffloaded marks a backend seam whose scoring is delegated to a remote /
// offloaded compute destination that is not wired in v1. It lets a caller
// distinguish "offloaded backend, results arrive out-of-band" from a real
// failure, without this shadow scaffold pretending to run heavy compute locally.
var ErrOffloaded = errors.New("routerbackend: scoring offloaded to a remote backend (not wired in v1)")

// RouterSubmission is the identity + drive input for one router scoring run. In
// v1 shadow the tasks are unused (adapters are stubs); the fields carry the
// submission identity the ledger entry is stamped with and the base URL the
// inclusion gate probes.
type RouterSubmission struct {
	MinerHotkey   string
	AgentID       string
	RouterBaseURL string
	FirstSeen     time.Time
	Tasks         []routerharness.Task
}

// RouterBackend scores one included submission into a routerscore.LedgerEntry.
// It is the interface -> [implementations] boundary: the compute destination
// (in-process, offloaded, remote) is a choice of implementation, not baked into
// the orchestration. Implementations may assume the inclusion gate has already
// passed (Dispatcher.Run enforces it).
type RouterBackend interface {
	Score(ctx context.Context, sub RouterSubmission) (routerscore.LedgerEntry, error)
}

// Dispatcher runs the shared opt-in gate, then delegates to a backend. It is the
// single choke point that guarantees the yes-and contract across every backend.
type Dispatcher struct {
	Backend RouterBackend
	// Get is the injected HTTP getter for the inclusion probe (nil -> the
	// default client). Injected so the gate is testable without a live server.
	Get func(url string) (*http.Response, error)
}

// Run applies the inclusion gate and dispatches.
//
//   - Not included (no router project) -> (result, zero entry, ErrNotIncluded):
//     the router run is skipped benignly; memory scoring is untouched.
//   - Included -> the backend scores the submission; its entry and error pass
//     through. An offloaded backend returns ErrOffloaded (results arrive
//     out-of-band).
//
// The InclusionResult is always returned (even on skip) so the caller can record
// the benign status for telemetry.
func (d Dispatcher) Run(
	ctx context.Context, sub RouterSubmission,
) (routerharness.InclusionResult, routerscore.LedgerEntry, error) {
	incl := routerharness.ProbeInclusion(ctx, sub.RouterBaseURL, d.Get)
	if !incl.Included {
		return incl, routerscore.LedgerEntry{}, ErrNotIncluded
	}
	entry, err := d.Backend.Score(ctx, sub)
	return incl, entry, err
}

// LocalBackend is the in-process implementation: it drives the harness adapters
// in this process and folds their outcomes through routerscore. In v1 shadow the
// adapters' RunTask methods are the errNotImplemented scaffold stubs, so every
// harness comes back non-operational and the entry folds to combined_score 0 —
// the honest shadow state. It is never weight-eligible (WeightEligible=false);
// the validator's SHADOW track state stays the authority.
type LocalBackend struct {
	Weights routerscore.HarnessWeights
	Gate    routerscore.FloorGate
	opts    []routerharness.Option
}

// NewLocalBackend builds the in-process backend with the default per-harness
// weights and the shadow floor gate. Options (e.g. WithCommandRunner) are
// forwarded to the adapters so tests can inject a fake toolchain.
func NewLocalBackend(opts ...routerharness.Option) *LocalBackend {
	return &LocalBackend{
		Weights: routerscore.DefaultHarnessWeights,
		Gate:    routerscore.ShadowFloorGate(),
		opts:    opts,
	}
}

// Score drives every harness adapter against the submission's router and folds
// the outcomes into a shadow ledger entry. This is the reachable caller that
// wires routerscore.BuildEntry into a real path; in v1 the adapter stubs make
// every slice forfeit, so the combined score is 0 and the entry is shadow-only.
func (b *LocalBackend) Score(
	ctx context.Context, sub RouterSubmission,
) (routerscore.LedgerEntry, error) {
	weights := b.Weights
	if weights == nil {
		weights = routerscore.DefaultHarnessWeights
	}
	var task routerharness.Task
	if len(sub.Tasks) > 0 {
		task = sub.Tasks[0]
	}
	adapters := routerharness.Adapters(b.opts...)
	outcomes := make([]routerscore.HarnessOutcome, 0, len(adapters))
	for _, adapter := range adapters {
		outcomes = append(outcomes, b.runOne(ctx, adapter, task))
	}
	// weightEligible is a defensive echo; the router track is SHADOW, so v1 is
	// always false. Promotion is a validator-side decision, never taken here.
	return routerscore.BuildEntry(
		sub.MinerHotkey, sub.AgentID, weights, false, sub.FirstSeen, outcomes,
	), nil
}

// runOne drives a single harness. In v1 the RunTask stub returns
// errNotImplemented, which yields a non-operational (forfeited) slice rather
// than a scoring fault, so a shadow run always produces a well-formed entry.
func (b *LocalBackend) runOne(
	ctx context.Context, adapter routerharness.HarnessAdapter, task routerharness.Task,
) routerscore.HarnessOutcome {
	h := adapter.Harness()
	operational, artifact, err := adapter.RunTask(ctx, task)
	if err != nil {
		// errNotImplemented (shadow scaffold) or any real fault: the slice
		// forfeits. Combine ignores a non-operational harness.
		return routerscore.HarnessOutcome{Harness: h, Operational: false}
	}
	floor := b.Gate.FloorPass(operational, artifact.Operational)
	return routerscore.HarnessOutcome{
		Harness:                 h,
		Operational:             operational,
		Floor:                   floor,
		Efficiency:              0,
		UpstreamTokenCostMicros: artifact.UpstreamTokenCostMicros,
	}
}

// OffloadedBackend is the documented seam for the default heavy path: scoring
// runs on a remote/offloaded compute destination and the ledger is published
// out-of-band (the validator only reads + folds it). It is not wired in v1;
// Score returns ErrOffloaded so the orchestration can record the hand-off
// without this scaffold pretending to run the work locally.
type OffloadedBackend struct{}

// Score reports that scoring is delegated out-of-band.
func (OffloadedBackend) Score(
	_ context.Context, _ RouterSubmission,
) (routerscore.LedgerEntry, error) {
	return routerscore.LedgerEntry{}, ErrOffloaded
}

var (
	_ RouterBackend = (*LocalBackend)(nil)
	_ RouterBackend = OffloadedBackend{}
)
