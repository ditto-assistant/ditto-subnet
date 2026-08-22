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
