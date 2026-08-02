package main

import (
	"math"
	"testing"
)

func approx(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestMean(t *testing.T) {
	if got := mean([]float64{1, 2, 3, 4}); !approx(got, 2.5) {
		t.Fatalf("mean = %v, want 2.5", got)
	}
	if got := mean(nil); got != 0 {
		t.Fatalf("mean(nil) = %v, want 0", got)
	}
}

func TestStddevSample(t *testing.T) {
	// Sample stddev (n-1) of {2,4,4,4,5,5,7,9} is exactly 2.13808993...
	xs := []float64{2, 4, 4, 4, 5, 5, 7, 9}
	if got := stddev(xs); math.Abs(got-2.138089935) > 1e-6 {
		t.Fatalf("stddev = %v, want ~2.13809", got)
	}
	// A constant series (a deterministic harness's noise floor) has stddev 0.
	if got := stddev([]float64{0.5, 0.5, 0.5}); got != 0 {
		t.Fatalf("stddev(constant) = %v, want 0", got)
	}
	// Fewer than two points → 0.
	if got := stddev([]float64{0.5}); got != 0 {
		t.Fatalf("stddev(single) = %v, want 0", got)
	}
}

func TestMinMax(t *testing.T) {
	xs := []float64{0.4, 0.1, 0.9, 0.3}
	if got := minf(xs); !approx(got, 0.1) {
		t.Fatalf("minf = %v, want 0.1", got)
	}
	if got := maxf(xs); !approx(got, 0.9) {
		t.Fatalf("maxf = %v, want 0.9", got)
	}
}

func TestField(t *testing.T) {
	ss := []sample{{toolMean: 0.2}, {toolMean: 0.8}}
	got := field(ss, func(s sample) float64 { return s.toolMean })
	if len(got) != 2 || got[0] != 0.2 || got[1] != 0.8 {
		t.Fatalf("field = %v", got)
	}
}
