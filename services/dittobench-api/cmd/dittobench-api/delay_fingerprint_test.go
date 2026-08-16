package main

import (
	"testing"
	"time"
)

func shadowConfig(minMS, maxMS int) delayFingerprintConfig {
	return delayFingerprintConfig{mode: delayFingerprintShadow, minMS: minMS, maxMS: maxMS}
}

func TestDelayFingerprintIsDeterministicAndBounded(t *testing.T) {
	key := make([]byte, delayFingerprintKeySize)
	for i := range key {
		key[i] = byte(i)
	}
	cfg := shadowConfig(25, 250)
	for generation := uint64(1); generation <= 3; generation++ {
		for ordinal := uint64(0); ordinal < 100; ordinal++ {
			first := delayFingerprintFor(key, generation, ordinal, cfg)
			second := delayFingerprintFor(key, generation, ordinal, cfg)
			if first != second {
				t.Fatalf("schedule is not deterministic at gen=%d ordinal=%d: %v != %v",
					generation, ordinal, first, second)
			}
			if first < 25*time.Millisecond || first > 250*time.Millisecond {
				t.Fatalf("delay %v outside [25ms, 250ms] at gen=%d ordinal=%d",
					first, generation, ordinal)
			}
		}
	}
}

func TestDelayFingerprintVariesAcrossOrdinalsAndKeys(t *testing.T) {
	keyA := make([]byte, delayFingerprintKeySize)
	keyB := make([]byte, delayFingerprintKeySize)
	keyB[0] = 1
	cfg := shadowConfig(25, 250)
	seen := map[time.Duration]struct{}{}
	for ordinal := uint64(0); ordinal < 64; ordinal++ {
		seen[delayFingerprintFor(keyA, 1, ordinal, cfg)] = struct{}{}
	}
	if len(seen) < 2 {
		t.Fatal("schedule is constant across ordinals; the fingerprint carries no signal")
	}
	// A different session key must produce a different schedule: the miner's
	// only route to predicting delays would be a key-independent schedule.
	same := 0
	for ordinal := uint64(0); ordinal < 64; ordinal++ {
		if delayFingerprintFor(keyA, 1, ordinal, cfg) == delayFingerprintFor(keyB, 1, ordinal, cfg) {
			same++
		}
	}
	if same == 64 {
		t.Fatal("schedule is identical under two different keys")
	}
}

func TestDelayFingerprintDisabledPaths(t *testing.T) {
	key := make([]byte, delayFingerprintKeySize)
	cases := []struct {
		name       string
		key        []byte
		generation uint64
		cfg        delayFingerprintConfig
	}{
		{"off mode", key, 1, delayFingerprintConfig{mode: delayFingerprintOff, minMS: 25, maxMS: 250}},
		{"nil key", nil, 1, shadowConfig(25, 250)},
		{"no case window", key, 0, shadowConfig(25, 250)},
		{"inverted range", key, 1, shadowConfig(250, 25)},
		{"zero max", key, 1, shadowConfig(0, 0)},
	}
	for _, tc := range cases {
		if got := delayFingerprintFor(tc.key, tc.generation, 0, tc.cfg); got != 0 {
			t.Fatalf("%s: expected no injection, got %v", tc.name, got)
		}
	}
}

func TestParseDelayFingerprintConfigDefaultsOff(t *testing.T) {
	cfg := parseDelayFingerprintConfig()
	if cfg.mode != delayFingerprintOff {
		t.Fatalf("default mode must be off, got %q", cfg.mode)
	}
	if cfg.enabled() {
		t.Fatal("default config must not inject")
	}
}

func TestParseDelayFingerprintConfigShadow(t *testing.T) {
	t.Setenv("DITTOBENCH_RELAY_DELAY_FP_MODE", "shadow")
	t.Setenv("DITTOBENCH_RELAY_DELAY_FP_MIN_MS", "40")
	t.Setenv("DITTOBENCH_RELAY_DELAY_FP_MAX_MS", "120")
	cfg := parseDelayFingerprintConfig()
	if cfg.mode != delayFingerprintShadow || cfg.minMS != 40 || cfg.maxMS != 120 {
		t.Fatalf("unexpected config: %+v", cfg)
	}
	if !cfg.enabled() {
		t.Fatal("shadow config with a valid range must inject")
	}
}

func TestParseDelayFingerprintConfigUnknownModeIsOff(t *testing.T) {
	t.Setenv("DITTOBENCH_RELAY_DELAY_FP_MODE", "enforce")
	if cfg := parseDelayFingerprintConfig(); cfg.mode != delayFingerprintOff {
		t.Fatalf("unknown mode must fall back to off, got %q", cfg.mode)
	}
}

func TestV9GenerationCaseDeltaRejectsDelayRegression(t *testing.T) {
	after := brokerCaseSnapshot{Requests: 2, Successes: 2, DelayedRequests: 3, InjectedDelayMS: 100}
	// DelayedRequests exceeding Successes is impossible under the booking
	// order (a delay is only ever booked beside a success) and must fail the
	// attribution closed rather than produce trusted-looking evidence.
	if observed, complete := v9GenerationCaseDelta(
		brokerCaseSnapshot{}, after, nil, nil,
	); observed || complete {
		t.Fatal("delayed-requests > successes must fail attribution closed")
	}
}

func TestV9GenerationCaseDeltaAcceptsV10OpeningSnapshot(t *testing.T) {
	// A v10+ case window opens with ToolEvidenceComplete already true (see
	// beginCaseCapability / the v10 beginCaseSnapshot branch). That bit is
	// initial metadata, not accrued activity, so an otherwise-empty opening
	// snapshot must settle attribution -- requiring the raw zero value rejected
	// every v10 case and forced the transitional base-evidence skip.
	before := brokerCaseSnapshot{ToolEvidenceComplete: true}
	after := brokerCaseSnapshot{ToolEvidenceComplete: true, Requests: 3, Successes: 3}
	observed, complete := v9GenerationCaseDelta(before, after, nil, nil)
	if !complete || !observed {
		t.Fatalf("v10 opening snapshot must settle complete attribution, got observed=%v complete=%v", observed, complete)
	}

	// The empty-window invariant on real counters is preserved: a window that
	// opens with carried-over request/success/in-flight activity still fails.
	dirty := brokerCaseSnapshot{ToolEvidenceComplete: true, Requests: 1}
	if _, complete := v9GenerationCaseDelta(dirty, after, nil, nil); complete {
		t.Fatal("a non-empty opening counter must still fail attribution closed")
	}
	inflight := brokerCaseSnapshot{ToolEvidenceComplete: true, InFlight: 1}
	if _, complete := v9GenerationCaseDelta(inflight, after, nil, nil); complete {
		t.Fatal("carried-over in-flight activity must still fail attribution closed")
	}
}

func TestV9RelayDelayEvidence(t *testing.T) {
	before := brokerCaseSnapshot{}
	noDelay := brokerCaseSnapshot{Requests: 1, Successes: 1}
	injected, verdict := v9RelayDelayEvidence(before, noDelay, 5000)
	if injected != 0 || verdict != nil {
		t.Fatalf("no delayed calls must be unmeasured, got injected=%d verdict=%v", injected, verdict)
	}

	delayed := brokerCaseSnapshot{Requests: 2, Successes: 2, DelayedRequests: 2, InjectedDelayMS: 300}
	injected, verdict = v9RelayDelayEvidence(before, delayed, 5000)
	if injected != 300 || verdict == nil || !*verdict {
		t.Fatalf("an honest slow case must be consistent, got injected=%d verdict=%v", injected, verdict)
	}

	// Wall time below the per-call mean of the injected delay: the harness
	// answered before the relay released a response its own window counts.
	injected, verdict = v9RelayDelayEvidence(before, delayed, 100)
	if injected != 300 || verdict == nil || *verdict {
		t.Fatalf("a too-fast case must be inconsistent, got injected=%d verdict=%v", injected, verdict)
	}

	// Two concurrent calls of 150ms each overlap: a 160ms case is legitimate
	// and the mean floor (150ms) must not flag it.
	injected, verdict = v9RelayDelayEvidence(before, delayed, 160)
	if injected != 300 || verdict == nil || !*verdict {
		t.Fatalf("overlapping holds must not flag an honest case, got injected=%d verdict=%v", injected, verdict)
	}
}
