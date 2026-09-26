package parserprobe

import (
	"errors"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
)

func TestParallelRouterFitMatchesSerialExactly(t *testing.T) {
	serial, err := trainRouter(13, "full", 2000000, 9, 1)
	if err != nil {
		t.Fatal(err)
	}
	for _, workers := range []int{2, 4} {
		parallel, err := trainRouter(13, "full", 2000000, 9, workers)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(serial, parallel) {
			t.Fatalf("%d workers changed the learned classifier", workers)
		}
	}
}

func TestRouterBatchReturnsFirstSeedErrorAndJoinsWorkers(t *testing.T) {
	var finished atomic.Int32
	got, err := routerTrainingBatch(40, 4, func(seed int64) (gen.DatasetArtifact, error) {
		defer finished.Add(1)
		if seed == 41 || seed == 43 {
			return gen.DatasetArtifact{}, errors.New("generation rejected")
		}
		return gen.DatasetArtifact{Seed: seed}, nil
	})
	if got != nil || err == nil || !strings.Contains(err.Error(), "seed 41:") || finished.Load() != 4 {
		t.Fatalf("partial fit or missing worker: %v %v %d", got, err, finished.Load())
	}
}

func TestRouterWorkerBounds(t *testing.T) {
	for _, workers := range []int{-1, 17} {
		if _, err := checkedOptions(Options{BenchVersion: 13, Seeds: 1, RouterWorkers: workers}); err == nil {
			t.Fatal("invalid worker count accepted")
		}
	}
	got, err := checkedOptions(Options{BenchVersion: 13, Seeds: 1})
	if err != nil || got.RouterWorkers != 1 {
		t.Fatalf("legacy default changed: %+v %v", got, err)
	}
}
