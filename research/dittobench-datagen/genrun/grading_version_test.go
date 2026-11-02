package genrun

import (
	"context"
	"strings"
	"testing"
)

func roundPlan(roundBench int) Plan {
	p := plan()
	p.RoundBenchVersion = roundBench
	return p
}

func TestEvaluateV13RoundIsTheOriginalContract(t *testing.T) {
	sb := Local{Limits: testLimits()}
	for _, round := range []int{0, 13} {
		ev, err := Evaluate(context.Background(), sb, fixture(t, "v14", false), roundPlan(round))
		if err != nil {
			t.Fatal(err)
		}
		if !ev.Valid() {
			t.Fatalf("round %d: %+v", round, ev)
		}
		for _, s := range append(ev.Seeds, *ev.Probe) {
			if s.GradingBenchVersion != 0 || s.LegacyRetry != "" {
				t.Fatalf("round %d: a v13 round must report the original seed output: %+v", round, s)
			}
		}
	}
}

func TestEvaluateV14Round(t *testing.T) {
	sb := Local{Limits: testLimits()}
	cases := map[string]struct {
		version int
		retried bool
	}{
		// A generator that never learned v14 ignores the new field.
		"ok": {13, false},
		// A version-aware generator declares v14.
		"v14": {14, false},
		// A strict decoder fails the new request and keeps its column on
		// the baseline request.
		"strict": {13, true},
		// A broken v14 path falls back to the valid v13 path.
		"v14broken": {13, true},
	}
	for name, want := range cases {
		ev, err := Evaluate(context.Background(), sb, fixture(t, name, false), roundPlan(14))
		if err != nil {
			t.Fatal(err)
		}
		if !ev.Valid() || ev.Deterministic == nil || !*ev.Deterministic {
			t.Fatalf("%s: %+v", name, ev)
		}
		for _, s := range append(ev.Seeds, *ev.Probe) {
			if s.GradingBenchVersion != want.version {
				t.Errorf("%s: grading version %d, want %d", name, s.GradingBenchVersion, want.version)
			}
			if (s.LegacyRetry != "") != want.retried {
				t.Errorf("%s: legacy retry %q", name, s.LegacyRetry)
			}
		}
	}
}

func TestEvaluateV14RoundReportsTheFirstFailure(t *testing.T) {
	ev, err := Evaluate(context.Background(), Local{Limits: testLimits()}, fixture(t, "crash", false), roundPlan(14))
	if err != nil {
		t.Fatal(err)
	}
	if ev.Valid() || !strings.Contains(ev.Seeds[0].Error, "exit 3") || ev.Seeds[0].LegacyRetry != "" {
		t.Fatalf("crash fixture: %+v", ev.Seeds[0])
	}
}
