package runner

import (
	"context"
	"errors"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestStageCasesByWaveClampsAndBucketsEveryCase(t *testing.T) {
	waves := []int{0, 2, -1, 7, 1, 2}
	buckets := StageCasesByWave(3, len(waves), func(i int) int { return waves[i] })
	if len(buckets) != 3 {
		t.Fatalf("buckets=%d, want 3", len(buckets))
	}
	want := [][]int{{0, 2}, {4}, {1, 3, 5}}
	for w := range want {
		if len(buckets[w]) != len(want[w]) {
			t.Fatalf("wave %d bucket %v, want %v", w, buckets[w], want[w])
		}
		for i := range want[w] {
			if buckets[w][i] != want[w][i] {
				t.Fatalf("wave %d bucket %v, want %v", w, buckets[w], want[w])
			}
		}
	}
	if got := StageCasesByWave(0, 2, func(int) int { return 5 }); len(got) != 1 || len(got[0]) != 2 {
		t.Fatalf("zero waves did not degrade to one bucket: %v", got)
	}
}

// TestRunStagedWavesSeedsThenRunsInOrder proves the barrier shape: seed is
// called only for waves that carry pairs, run follows its wave's seed, and no
// later wave's seed precedes an earlier wave's run.
func TestRunStagedWavesSeedsThenRunsInOrder(t *testing.T) {
	waves := []protocol.SeedRequest{
		{Wave: 0, Pairs: []protocol.MemoryPair{{PairID: "a"}}},
		{Wave: 1},
		{Wave: 2, Pairs: []protocol.MemoryPair{{PairID: "b"}}},
	}
	buckets := [][]int{{0, 1}, {2}, {3}}
	var events []string
	err := RunStagedWaves(context.Background(), waves, buckets,
		func(_ context.Context, w int, req protocol.SeedRequest) error {
			events = append(events, "seed"+string(rune('0'+w))+":"+req.Pairs[0].PairID)
			return nil
		},
		func(_ context.Context, w int, bucket []int) error {
			events = append(events, "run"+string(rune('0'+w))+":"+string(rune('0'+len(bucket))))
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"seed0:a", "run0:2", "run1:1", "seed2:b", "run2:1"}
	if len(events) != len(want) {
		t.Fatalf("events %v, want %v", events, want)
	}
	for i := range want {
		if events[i] != want[i] {
			t.Fatalf("events %v, want %v", events, want)
		}
	}
}

func TestRunStagedWavesReportsTheFailingWaveAndStops(t *testing.T) {
	waves := []protocol.SeedRequest{
		{Pairs: []protocol.MemoryPair{{PairID: "a"}}},
		{Pairs: []protocol.MemoryPair{{PairID: "b"}}},
	}
	boom := errors.New("embedding service unavailable")
	runs := 0
	err := RunStagedWaves(context.Background(), waves, [][]int{{0}, {1}},
		func(_ context.Context, w int, _ protocol.SeedRequest) error {
			if w == 1 {
				return boom
			}
			return nil
		},
		func(_ context.Context, _ int, _ []int) error { runs++; return nil })
	var seedErr *WaveSeedError
	if !errors.As(err, &seedErr) || seedErr.Wave != 1 || !errors.Is(err, boom) {
		t.Fatalf("err=%v, want wave-1 seed error wrapping the cause", err)
	}
	if runs != 1 {
		t.Fatalf("ran %d waves after the failed seed, want the wave-1 bucket never dispatched", runs)
	}

	stop := errors.New("projection failure")
	err = RunStagedWaves(context.Background(), waves, [][]int{{0}, {1}},
		func(_ context.Context, _ int, _ protocol.SeedRequest) error { return nil },
		func(_ context.Context, _ int, _ []int) error { return stop })
	if !errors.Is(err, stop) {
		t.Fatalf("run error not propagated: %v", err)
	}
	if errors.As(err, &seedErr) {
		t.Fatal("a run error must not read as a seed error")
	}
}

func TestRunStagedWavesHonorsCancellationBetweenWaves(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	waves := []protocol.SeedRequest{{Pairs: []protocol.MemoryPair{{PairID: "a"}}}, {Pairs: []protocol.MemoryPair{{PairID: "b"}}}}
	seeds := 0
	err := RunStagedWaves(ctx, waves, [][]int{{0}, {1}},
		func(_ context.Context, _ int, _ protocol.SeedRequest) error { seeds++; return nil },
		func(_ context.Context, _ int, _ []int) error { cancel(); return nil })
	if !errors.Is(err, context.Canceled) || seeds != 1 {
		t.Fatalf("err=%v seeds=%d, want cancellation after wave 0 with wave 1 never seeded", err, seeds)
	}
}
