package parserprobe

import (
	"math"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
)

func TestProbeSeedIsolation(t *testing.T) {
	base := Options{BenchVersion: 13, FirstSeed: 10, Seeds: 40, RouterSeeds: 10000, RouterFirstSeed: 2000000}
	for _, first := range []int64{10, 49, -9989} {
		opts := base
		opts.RouterFirstSeed = first
		if _, err := checkedOptions(opts); err == nil {
			t.Errorf("overlap starting %d accepted", first)
		}
	}
	for _, first := range []int64{-9990, 50, 2000000} {
		opts := base
		opts.RouterFirstSeed = first
		if _, err := checkedOptions(opts); err != nil {
			t.Errorf("disjoint starting %d rejected: %v", first, err)
		}
	}
	base.RouterFirstSeed = 0
	got, err := checkedOptions(base)
	if err != nil || got.RouterFirstSeed != 1050 {
		t.Fatalf("default range: %+v %v", got, err)
	}
}

func TestArtifactTrainingIsolation(t *testing.T) {
	base := Options{BenchVersion: 13, RouterSeeds: 10000, RouterFirstSeed: 2000000,
		Artifacts: []gen.DatasetArtifact{{BenchVersion: 13, Seed: 2009999}}}
	if _, err := checkedOptions(base); err == nil {
		t.Fatal("training overlap with artifact accepted")
	}
	base.RouterFirstSeed = 0
	got, err := checkedOptions(base)
	if err != nil || got.RouterFirstSeed != 2011000 {
		t.Fatalf("default must follow actual artifact seeds, not CLI seed defaults: %+v %v", got, err)
	}
	base.Artifacts = append(base.Artifacts, base.Artifacts[0])
	if _, err := checkedOptions(base); err == nil {
		t.Fatal("duplicate artifact seed accepted")
	}
	base.Artifacts = base.Artifacts[:1]
	base.Artifacts[0].BenchVersion = 12
	if _, err := checkedOptions(base); err == nil {
		t.Fatal("mixed benchmark version accepted")
	}
}

func TestProbeRangeValidationBeforeTraining(t *testing.T) {
	for _, opts := range []Options{
		{Seeds: 1, RouterSeeds: -1},
		{Seeds: 0},
		{Seeds: 2, FirstSeed: math.MaxInt64},
		{Seeds: 1, FirstSeed: math.MaxInt64, RouterSeeds: 1},
		{Seeds: 1, RouterSeeds: 2, RouterFirstSeed: math.MaxInt64},
		{Seeds: 40, FirstSeed: 1, RouterSeeds: 10000, RouterFirstSeed: 20},
	} {
		// Invalid configuration must fail before profile lookup or expensive
		// training, even though this fixture intentionally has no version.
		if _, err := Run(opts); err == nil {
			t.Fatalf("invalid options accepted: %+v", opts)
		}
	}
}
