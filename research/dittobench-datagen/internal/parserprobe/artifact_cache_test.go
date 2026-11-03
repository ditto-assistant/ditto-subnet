package parserprobe

import (
	"fmt"
	"runtime"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
)

// The controls below each read a few full-profile artifacts, often the same
// (seed, version) pairs. probeArtifacts generates each pair once per test
// binary, concurrently, and shares it; callers only read the artifacts.

type probeKey struct {
	seed    int64
	version int
}

type probeEntry struct {
	once     sync.Once
	artifact gen.DatasetArtifact
	err      error
}

var probeCache sync.Map // probeKey -> *probeEntry

func loadProbeArtifact(k probeKey) (gen.DatasetArtifact, error) {
	v, _ := probeCache.LoadOrStore(k, &probeEntry{})
	e := v.(*probeEntry)
	e.once.Do(func() {
		prof, ok := gen.ProfileForVersion("full", k.version)
		if !ok {
			e.err = fmt.Errorf("no full profile for v%d", k.version)
			return
		}
		e.artifact, e.err = gen.GenerateDataset(k.seed, prof, k.version)
	})
	return e.artifact, e.err
}

// probeArtifacts returns the full-profile artifacts for seeds, in order.
func probeArtifacts(t *testing.T, version int, seeds ...int64) []gen.DatasetArtifact {
	t.Helper()
	out := make([]gen.DatasetArtifact, len(seeds))
	errs := make([]error, len(seeds))
	sem := make(chan struct{}, runtime.GOMAXPROCS(0))
	var wg sync.WaitGroup
	for i, seed := range seeds {
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			out[i], errs[i] = loadProbeArtifact(probeKey{seed, version})
		}()
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("v%d seed %d: %v", version, seeds[i], err)
		}
	}
	return out
}

// seedRange returns first..last inclusive.
func seedRange(first, last int64) []int64 {
	seeds := make([]int64, 0, last-first+1)
	for seed := first; seed <= last; seed++ {
		seeds = append(seeds, seed)
	}
	return seeds
}
