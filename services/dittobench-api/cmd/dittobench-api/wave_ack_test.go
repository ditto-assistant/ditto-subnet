package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// ingestingHarness is a fake harness whose /seed takes real time to ingest
// (it sleeps before marking the pairs queryable and returning 2xx) and whose
// /run checks that every record the case depends on was ingested BEFORE the
// request arrived. A dispatch that beats the ingest ack is recorded as a
// violation, which is exactly the race an honest harness would lose to.
type ingestingHarness struct {
	mu          sync.Mutex
	ingestLag   time.Duration
	required    map[string][]string // case id -> required pair ids
	waveOfCase  map[string]int
	ingested    map[string]bool
	seedAcks    []time.Time
	runs        []harnessRun
	violations  []string
	seedsInUse  int
	inFlight    int
	maxInFlight int
}

type harnessRun struct {
	caseID  string
	wave    int
	arrived time.Time
	done    time.Time
}

func newIngestingHarness(lag time.Duration, cases []gen.StagedCase) *ingestingHarness {
	h := &ingestingHarness{
		ingestLag:  lag,
		required:   map[string][]string{},
		waveOfCase: map[string]int{},
		ingested:   map[string]bool{},
	}
	for _, sc := range cases {
		h.required[sc.Case.ID] = sc.RequiredPairIDs
		h.waveOfCase[sc.Case.ID] = sc.RunAfterWave
	}
	return h
}

func (h *ingestingHarness) reset() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.ingested = map[string]bool{}
	h.seedAcks = nil
	h.runs = nil
	h.violations = nil
	h.maxInFlight = 0
}

func (h *ingestingHarness) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/health":
			w.WriteHeader(http.StatusOK)
		case "/seed":
			var req protocol.SeedRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			h.mu.Lock()
			h.seedsInUse++
			h.mu.Unlock()
			// Embedding takes time; the pairs become queryable only at the end.
			time.Sleep(h.ingestLag)
			h.mu.Lock()
			for _, pair := range req.Pairs {
				h.ingested[pair.PairID] = true
			}
			h.seedAcks = append(h.seedAcks, time.Now())
			h.seedsInUse--
			h.mu.Unlock()
			_ = json.NewEncoder(w).Encode(protocol.SeedResponse{Pairs: len(req.Pairs)})
		case "/run":
			var req protocol.RunRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			arrived := time.Now()
			h.mu.Lock()
			h.inFlight++
			if h.inFlight > h.maxInFlight {
				h.maxInFlight = h.inFlight
			}
			if h.seedsInUse > 0 {
				h.violations = append(h.violations, fmt.Sprintf("case %s arrived while a /seed was still ingesting", req.CaseID))
			}
			for _, pairID := range h.required[req.CaseID] {
				if !h.ingested[pairID] {
					h.violations = append(h.violations, fmt.Sprintf("case %s (wave %d) arrived before pair %s was ingested", req.CaseID, h.waveOfCase[req.CaseID], pairID))
					break
				}
			}
			h.mu.Unlock()
			time.Sleep(time.Millisecond)
			h.mu.Lock()
			h.inFlight--
			h.runs = append(h.runs, harnessRun{caseID: req.CaseID, wave: h.waveOfCase[req.CaseID], arrived: arrived, done: time.Now()})
			h.mu.Unlock()
			_ = json.NewEncoder(w).Encode(protocol.RunResponse{FinalText: "ok"})
		default:
			http.NotFound(w, r)
		}
	})
}

// v13WaveFixture generates the v13 full memory suite plus the tool
// prerequisite wave, the way runSizeJob does.
func v13WaveFixture(t *testing.T) (protocol.SeedRequest, gen.MemorySuite) {
	t.Helper()
	const seed = int64(123456789)
	prof, ok := gen.ProfileForVersion("full", protocol.BenchVersionV13)
	if !ok {
		t.Fatal("no v13 full profile")
	}
	rng, err := gen.NewRNGForVersion(seed, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	tools, _ := gen.GenerateToolsForVersion(rng, seed, prof.Tools, protocol.BenchVersionV13)
	suite, err := gen.GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	prerequisites, err := toolPrerequisiteWave(tools)
	if err != nil {
		t.Fatal(err)
	}
	return prerequisites, suite
}

// dispatchWaves drives the same barrier runSizeJob uses: runner.RunStagedWaves
// with the runtime's runBounded at the requested case_concurrency.
func dispatchWaves(ctx context.Context, harnessURL string, suite gen.MemorySuite, concurrency int) error {
	buckets := runner.StageCasesByWave(suite.SeedingWaves, len(suite.Cases), func(i int) int { return suite.Cases[i].RunAfterWave })
	return runner.RunStagedWaves(ctx, suite.Waves, buckets,
		func(ctx context.Context, _ int, wave protocol.SeedRequest) error {
			_, err := runner.SeedForVersion(ctx, harnessURL, wave, protocol.BenchVersionV13)
			return err
		},
		func(ctx context.Context, _ int, bucket []int) error {
			runBounded(ctx, len(bucket), concurrency, func(i int) {
				sc := suite.Cases[bucket[i]]
				_, _ = runner.RunCase(ctx, harnessURL, sc.Case.ID, sc.Case.Question, nil, runner.CaseOptions{UserID: gen.PrimaryUser, BenchVersion: protocol.BenchVersionV13})
			})
			return ctx.Err()
		})
}

// TestWaveDispatchHonorsIngestAckUnderCaseConcurrency is the #1844 ordering
// proof: on a real v13 full dataset, at every case_concurrency the runtime
// accepts, no case is dispatched before the /seed that delivers its evidence
// has been acknowledged, no case of wave w+1 starts before every wave-w case has
// finished, and concurrency inside a wave is genuinely used.
func TestWaveDispatchHonorsIngestAckUnderCaseConcurrency(t *testing.T) {
	runner.Configure(true)
	t.Cleanup(func() { runner.Configure(false) })
	prerequisites, suite := v13WaveFixture(t)
	staged := 0
	for _, sc := range suite.Cases {
		if sc.RunAfterWave > 0 {
			staged++
		}
	}
	if staged == 0 {
		t.Fatal("v13 full suite stages no case into a later wave; the barrier would be untested")
	}
	harness := newIngestingHarness(40*time.Millisecond, suite.Cases)
	srv := httptest.NewServer(harness.handler())
	defer srv.Close()

	for _, concurrency := range []int{1, 4, 16, maxBenchmarkCaseConcurrency} {
		harness.reset()
		ctx := context.Background()
		if _, err := runner.SeedForVersion(ctx, srv.URL, prerequisites, protocol.BenchVersionV13); err != nil {
			t.Fatalf("concurrency %d: prerequisite seed: %v", concurrency, err)
		}
		if err := dispatchWaves(ctx, srv.URL, suite, concurrency); err != nil {
			t.Fatalf("concurrency %d: %v", concurrency, err)
		}
		harness.mu.Lock()
		runs, acks, violations, maxInFlight := harness.runs, harness.seedAcks, harness.violations, harness.maxInFlight
		harness.mu.Unlock()
		if len(violations) > 0 {
			t.Fatalf("concurrency %d: %d ordering violations, first: %s", concurrency, len(violations), violations[0])
		}
		if len(runs) != len(suite.Cases) {
			t.Fatalf("concurrency %d: %d cases ran, want %d", concurrency, len(runs), len(suite.Cases))
		}
		// Every wave-w case arrived after wave w's ack and after every wave-(w-1)
		// case finished; wave w's ack came after every earlier wave's cases.
		nonEmpty := 0
		for _, wave := range suite.Waves {
			if len(wave.Pairs) > 0 {
				nonEmpty++
			}
		}
		if len(acks) != nonEmpty+1 { // + the prerequisite seed
			t.Fatalf("concurrency %d: %d seed acks, want %d", concurrency, len(acks), nonEmpty+1)
		}
		lastDone := map[int]time.Time{}
		firstArrival := map[int]time.Time{}
		for _, run := range runs {
			if run.done.After(lastDone[run.wave]) {
				lastDone[run.wave] = run.done
			}
			if first, ok := firstArrival[run.wave]; !ok || run.arrived.Before(first) {
				firstArrival[run.wave] = run.arrived
			}
		}
		for w := 1; w < suite.SeedingWaves; w++ {
			first, ok := firstArrival[w]
			if !ok {
				continue
			}
			if prev, ok := lastDone[w-1]; ok && first.Before(prev) {
				t.Fatalf("concurrency %d: a wave-%d case started before wave %d finished", concurrency, w, w-1)
			}
		}
		if concurrency > 1 && maxInFlight < 2 {
			t.Fatalf("concurrency %d: cases never overlapped inside a wave (max in flight %d)", concurrency, maxInFlight)
		}
		if concurrency == 1 && maxInFlight != 1 {
			t.Fatalf("serial dispatch overlapped %d cases", maxInFlight)
		}
	}
}

// TestWaveDispatchWithoutTheAckLosesTheIngestRace is the control: the same
// harness, the same dataset, but the dependent cases fired concurrently with
// the wave's /seed instead of after its ack. The detector fires, which is what
// makes the passing test above meaningful, and shows the race an honest
// harness would lose without the barrier.
func TestWaveDispatchWithoutTheAckLosesTheIngestRace(t *testing.T) {
	runner.Configure(true)
	t.Cleanup(func() { runner.Configure(false) })
	prerequisites, suite := v13WaveFixture(t)
	harness := newIngestingHarness(40*time.Millisecond, suite.Cases)
	srv := httptest.NewServer(harness.handler())
	defer srv.Close()
	ctx := context.Background()
	if _, err := runner.SeedForVersion(ctx, srv.URL, prerequisites, protocol.BenchVersionV13); err != nil {
		t.Fatal(err)
	}
	buckets := runner.StageCasesByWave(suite.SeedingWaves, len(suite.Cases), func(i int) int { return suite.Cases[i].RunAfterWave })
	for w, wave := range suite.Waves {
		var wg sync.WaitGroup
		if len(wave.Pairs) > 0 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				_, _ = runner.SeedForVersion(ctx, srv.URL, wave, protocol.BenchVersionV13)
			}()
		}
		// Racing dispatcher: does not wait for the ack.
		runBounded(ctx, len(buckets[w]), maxBenchmarkCaseConcurrency, func(i int) {
			sc := suite.Cases[buckets[w][i]]
			_, _ = runner.RunCase(ctx, srv.URL, sc.Case.ID, sc.Case.Question, nil, runner.CaseOptions{UserID: gen.PrimaryUser, BenchVersion: protocol.BenchVersionV13})
		})
		wg.Wait()
	}
	harness.mu.Lock()
	violations := len(harness.violations)
	harness.mu.Unlock()
	if violations == 0 {
		t.Fatal("racing dispatcher produced no ordering violation; the detector cannot see the ingest race")
	}
}
