package gen

import (
	"fmt"
	"os"
	"runtime"
	"sort"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Generating a full v13/v14 artifact is the dominant cost of this package's
// tests, and many assertions read the same (seed, run size, version, salt)
// tuple. CachedDataset generates each tuple once per test binary and hands
// every caller the same artifact, so an assertion added over the shared
// qualification seeds costs only its own checks.
//
// Callers must treat the artifact as read-only. TestMain re-hashes every
// cached artifact after the run and fails the binary if any test mutated one,
// so a careless in-place edit cannot leak into another test's view.

type datasetKey struct {
	seed    int64
	runSize string
	version int
	salt    uint64
}

type datasetEntry struct {
	once     sync.Once
	artifact DatasetArtifact
	sha      string
	err      error
}

var datasetCache sync.Map // datasetKey -> *datasetEntry

func cachedDataset(t testing.TB, seed int64, runSize string, version int, salt uint64) DatasetArtifact {
	t.Helper()
	a, err := loadCachedDataset(datasetKey{seed, runSize, version, salt})
	if err != nil {
		t.Fatal(err)
	}
	return a
}

// CachedDataset exposes the cache to the external gen_test package.
func CachedDataset(t testing.TB, seed int64, runSize string, version int) DatasetArtifact {
	t.Helper()
	return cachedDataset(t, seed, runSize, version, 0)
}

func loadCachedDataset(k datasetKey) (DatasetArtifact, error) {
	v, _ := datasetCache.LoadOrStore(k, &datasetEntry{})
	e := v.(*datasetEntry)
	e.once.Do(func() {
		prof, ok := ProfileForVersion(k.runSize, k.version)
		if !ok {
			e.err = fmt.Errorf("no %s profile for v%d", k.runSize, k.version)
			return
		}
		e.artifact, e.err = GenerateDatasetWithSurface(k.seed, prof, k.version, SurfaceOptions{Salt: k.salt})
		if e.err != nil {
			e.err = fmt.Errorf("v%d %s seed %d salt %d: %w", k.version, k.runSize, k.seed, k.salt, e.err)
			return
		}
		e.sha, _, e.err = e.artifact.SHA256Hex()
	})
	return e.artifact, e.err
}

// cachedDatasets returns the artifacts for seeds first..last in seed order,
// generating missing ones concurrently (generation is goroutine-safe and
// deterministic per tuple).
func cachedDatasets(t testing.TB, first, last int64, runSize string, version int, salt uint64) []DatasetArtifact {
	t.Helper()
	n := int(last - first + 1)
	out := make([]DatasetArtifact, n)
	errs := make([]error, n)
	sem := make(chan struct{}, runtime.GOMAXPROCS(0))
	var wg sync.WaitGroup
	for i := range n {
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			out[i], errs[i] = loadCachedDataset(datasetKey{first + int64(i), runSize, version, salt})
		}()
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	return out
}

// v13FortySeeds is the pinned 40 full-profile v13 qualification seeds.
func v13FortySeeds(t testing.TB) []DatasetArtifact {
	t.Helper()
	return cachedDatasets(t, 1, 40, "full", protocol.BenchVersionV13, 0)
}

// v13MediumSeeds is the medium-profile counterpart for per-seed invariants
// that also bind the medium envelope. Medium uses the same allocators as full,
// so a short sweep is enough.
func v13MediumSeeds(t testing.TB) []DatasetArtifact {
	t.Helper()
	return cachedDatasets(t, 1, 8, "medium", protocol.BenchVersionV13, 0)
}

// v13SaltedSeeds is the private-surface (salt 91) counterpart of the
// qualification seeds, for invariants that must survive surface
// randomization. The salt re-renders only surfaces, so a short sweep suffices.
func v13SaltedSeeds(t testing.TB) []DatasetArtifact {
	t.Helper()
	return cachedDatasets(t, 1, 8, "full", protocol.BenchVersionV13, 91)
}

func TestMain(m *testing.M) {
	code := m.Run()
	if mutated := mutatedCachedDatasets(); len(mutated) > 0 {
		fmt.Fprintf(os.Stderr, "FAIL: tests mutated shared cached artifacts: %v\n", mutated)
		code = 1
	}
	os.Exit(code)
}

// mutatedCachedDatasets re-hashes every cached artifact and names any whose
// bytes no longer match the hash taken when it was generated.
func mutatedCachedDatasets() []string {
	var mutated []string
	datasetCache.Range(func(key, value any) bool {
		k, e := key.(datasetKey), value.(*datasetEntry)
		if e.err != nil {
			return true
		}
		if sha, _, err := e.artifact.SHA256Hex(); err != nil || sha != e.sha {
			mutated = append(mutated, fmt.Sprintf("v%d %s seed %d salt %d", k.version, k.runSize, k.seed, k.salt))
		}
		return true
	})
	sort.Strings(mutated)
	return mutated
}
