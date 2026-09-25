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
	"github.com/ditto-assistant/dittobench-api/internal/routerreplay"
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

// LocalBackend is the in-process implementation: it replays the embedded offline
// corpus (internal/routerreplay) through routerscore's token-dominant blend and
// folds a shadow ledger entry. No live provider is called — v1 shadow is offline
// determinism/prefix replay — so the entry is a real, non-zero shadow composite
// while remaining never weight-eligible (WeightEligible=false). The validator's
// SHADOW track state stays the authority. If the corpus fails to load the backend
// degrades safely to an all-forfeit entry (combined 0), never a scoring fault.
type LocalBackend struct {
	Weights   routerscore.HarnessWeights
	AxisWeights routerscore.EffAxisWeights
	Gate      routerscore.FloorGate
	corpus    routerreplay.Corpus
	corpusErr error
}

// NewLocalBackend builds the in-process backend with the default per-harness
// weights, the token-dominant axis weights, and the shadow floor gate, loading
// the embedded reference replay corpus. A corpus load error is retained and
// surfaces as a safe all-forfeit entry, never a panic.
func NewLocalBackend() *LocalBackend {
	corpus, err := routerreplay.Default()
	return &LocalBackend{
		Weights:     routerscore.DefaultHarnessWeights,
		AxisWeights: routerscore.DefaultEffAxisWeights,
		Gate:        routerscore.ShadowFloorGate(),
		corpus:      corpus,
		corpusErr:   err,
	}
}

// Score replays the offline corpus and folds the outcomes into a shadow ledger
// entry. The measured aggregate lands in ShadowComposite; CombinedScore (the only
// number the validator folds) stays 0 because the entry is never weight-eligible.
func (b *LocalBackend) Score(
	_ context.Context, sub RouterSubmission,
) (routerscore.LedgerEntry, error) {
	weights := b.Weights
	if weights == nil {
		weights = routerscore.DefaultHarnessWeights
	}
	axis := b.AxisWeights
	if (axis == routerscore.EffAxisWeights{}) {
		axis = routerscore.DefaultEffAxisWeights
	}
	outcomes := b.outcomes(axis)
	// weightEligible is a defensive echo; the router track is SHADOW, so v1 is
	// always false. Promotion is a validator-side decision, never taken here.
	return routerscore.BuildEntry(
		sub.MinerHotkey, sub.AgentID, weights, false, sub.FirstSeen, outcomes,
	), nil
}

// outcomes returns the replayed per-harness outcomes, or — when the corpus failed
// to load — one non-operational (forfeited) slice per harness so the entry stays
// well-formed and folds to a combined 0.
func (b *LocalBackend) outcomes(axis routerscore.EffAxisWeights) []routerscore.HarnessOutcome {
	if b.corpusErr == nil && len(b.corpus.Harnesses) > 0 {
		return b.corpus.Outcomes(axis, b.Gate)
	}
	harnesses := routerharness.Harnesses()
	forfeit := make([]routerscore.HarnessOutcome, 0, len(harnesses))
	for _, h := range harnesses {
		forfeit = append(forfeit, routerscore.HarnessOutcome{Harness: h, Operational: false})
	}
	return forfeit
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
