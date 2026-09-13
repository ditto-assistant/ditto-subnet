package runner

import (
	"context"
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Staged seeding waves and the ingest acknowledgement.
//
// A memory suite may split its haystack into waves: /seed wave 0 (plus the
// tool prerequisites) before any case, then later waves interleaved with /run.
// The harness's 2xx on POST /seed is its INGEST ACKNOWLEDGEMENT: it means every
// pair in that request is embedded, indexed, and answerable, not merely
// received. The runner relies on it as a barrier. A case whose evidence
// arrives in wave w (StagedCase.RunAfterWave == w) is dispatched only after
// wave w's ack, and wave w+1 is not sent until every wave-w case has finished.
//
// Without that barrier an honest harness loses credit to a race it cannot see:
// a case dispatched while the harness is still embedding wave w's records
// answers from a store that does not yet hold them and grades 0, exactly as a
// fabricating harness would. Bench v13 stages real corrections into waves 1-2
// (research/dittobench-datagen, universe.StagedCorrectionWaves), so the barrier
// is load-bearing there; for v2-v12, where every case runs after wave 0, it
// degrades to seed-then-run-all.
//
// RunStagedWaves is the single implementation of that barrier. cmd/dittobench-api
// drives the scored memory phase through it, and the integration tests under
// cmd/dittobench-api prove the ordering at every case_concurrency the runtime
// accepts (1..64).

// WaveSeedError reports which wave's /seed failed. The wrapped error is the
// SeedForVersion error, so callers keep their typed checks (for example
// ErrSeedStoreLockTimeout).
type WaveSeedError struct {
	Wave int
	Err  error
}

func (e *WaveSeedError) Error() string {
	return fmt.Sprintf("seeding haystack wave %d failed: %v", e.Wave, e.Err)
}

func (e *WaveSeedError) Unwrap() error { return e.Err }

// StageCasesByWave buckets case indices [0,n) by the wave each runs after,
// clamping out-of-range values into [0,nWaves) exactly as the runtime always
// has (a negative wave runs first, a wave past the last runs last). Every
// bucket exists even when empty so the caller can iterate waves uniformly.
func StageCasesByWave(nWaves, n int, runAfterWave func(i int) int) [][]int {
	if nWaves < 1 {
		nWaves = 1
	}
	buckets := make([][]int, nWaves)
	for i := 0; i < n; i++ {
		w := runAfterWave(i)
		if w < 0 {
			w = 0
		}
		if w >= nWaves {
			w = nWaves - 1
		}
		buckets[w] = append(buckets[w], i)
	}
	return buckets
}

// RunStagedWaves executes the wave barrier. For each wave in order it calls
// seed (only when the wave carries pairs, matching the historical runtime) and
// returns a *WaveSeedError if that fails; then it calls run with the wave's
// bucket, which must dispatch every listed case and return only once all have
// finished (or return the reason it could not). A non-nil run error stops the
// loop and is returned as-is; ctx cancellation is checked between waves.
//
// The ack is the seed call returning nil: SeedForVersion returns only after the
// harness's 2xx, so run for wave w can never observe a case dispatched before
// the harness acknowledged ingesting wave w.
func RunStagedWaves(
	ctx context.Context,
	waves []protocol.SeedRequest,
	buckets [][]int,
	seed func(ctx context.Context, wave int, req protocol.SeedRequest) error,
	run func(ctx context.Context, wave int, bucket []int) error,
) error {
	for w, wave := range waves {
		if err := ctx.Err(); err != nil {
			return err
		}
		if len(wave.Pairs) > 0 {
			if err := seed(ctx, w, wave); err != nil {
				return &WaveSeedError{Wave: w, Err: err}
			}
		}
		var bucket []int
		if w < len(buckets) {
			bucket = buckets[w]
		}
		if err := run(ctx, w, bucket); err != nil {
			return err
		}
	}
	return nil
}
