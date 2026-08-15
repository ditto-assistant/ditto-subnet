package main

// The relay delay fingerprint makes the latency channel itself trustworthy
// evidence of model use.
//
// Why timing needs a watermark
// ----------------------------
//
// The v9 model-use gate already attributes every successful chat completion to
// the exact case window it started in (beginCaseSnapshot / endCaseSnapshot),
// so "did this case reach the model at all" is settled by trusted counters.
// What the counters cannot see is the mirror-image evasion the platform's
// model-use rule documents as needing "response-conditioned evidence": a
// harness that answers a case deterministically while its latency LOOKS like
// honest inference, either because the deterministic path is padded with
// sleeps or because a real call was fired and its response ignored.
//
// A sleep is a perfect forgery of latency only while latency is unstructured.
// This module gives every relayed completion a deterministic, secret-keyed
// response delay: the broker holds each successful upstream response for
// HMAC(session key, case generation, ordinal) milliseconds before releasing
// it to the harness. The schedule is exactly reproducible by the trusted side
// and unpredictable to the miner, because the key never leaves this process's
// memory -- the same custody rule the broker already applies to the platform
// bearer and DPoP material.
//
// What that buys:
//
//   - A case that actually waited on the relay embeds the injected delay in
//     its wall time; the scorer can subtract the schedule it knows and reason
//     about the residual.
//   - A case that faked its latency with sleeps cannot embed the schedule,
//     because the miner cannot compute it: the per-case injected total is a
//     fresh HMAC output per (session, generation, ordinal), and sessions are
//     minted per run.
//   - A harness that fires a call and returns before the broker releases the
//     response is directly visible: its case wall time is smaller than the
//     delay the broker verifiably imposed inside that case's window
//     (v9RelayDelayEvidence).
//
// What it deliberately does not do yet: change any score. The mode ladder is
// the shadow-before-enforce precedent that ditto-platform#506 invariant 5
// requires of every new gate -- inject and record first, publish the evidence,
// and only gate once the honest-cohort distribution is measured. The evidence
// stays in the run transcript (runner.CaseExecution) and operator logs; the
// signed v9 gate models are untouched, so no protocol version negotiation is
// involved.
//
// Cost to honest runs: the mean injected delay is (min+max)/2 per successful
// call, ~2 calls per case for the honest cohort, so the defaults add ~275ms
// to a case whose honest p50 is multiple seconds -- well inside the 5-minute
// per-case budget and indistinguishable from ordinary provider variance.

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"strings"
	"time"
)

const (
	// delayFingerprintKeySize is the per-session HMAC key length. The key is
	// minted with the session in prepare() and lives only in broker memory.
	delayFingerprintKeySize = 32
	// delayFingerprintDomain separates this HMAC use from every other keyed
	// construction in the broker (tool capabilities use their own key AND
	// their own domain string; sharing neither is deliberate).
	delayFingerprintDomain = "dittobench-delay-fp-v1"
)

type delayFingerprintMode string

const (
	// delayFingerprintOff injects nothing and records nothing. Default: the
	// threshold must be published before it bites, and the injection must be
	// announced before it is measured.
	delayFingerprintOff delayFingerprintMode = "off"
	// delayFingerprintShadow injects the scheduled delay and records the
	// per-case totals as transcript evidence; never changes a score.
	delayFingerprintShadow delayFingerprintMode = "shadow"
)

type delayFingerprintConfig struct {
	mode  delayFingerprintMode
	minMS int
	maxMS int
}

// enabled reports whether the schedule should inject at all. A misconfigured
// range disables injection rather than clamping: a silent clamp would make the
// operator's published parameters and the realized schedule disagree, and the
// whole point of the fingerprint is that the trusted side can state exactly
// what it injected.
func (c delayFingerprintConfig) enabled() bool {
	return c.mode == delayFingerprintShadow && c.minMS >= 0 && c.maxMS >= c.minMS && c.maxMS > 0
}

// parseDelayFingerprintConfig builds the config from the environment, following
// the broker's existing envOr/envIntDefault conventions. Unknown modes fall
// back to off for the same reason unknown platform decline codes fall back to
// platform-fault: the unrecognized value must land in the safe bin.
func parseDelayFingerprintConfig() delayFingerprintConfig {
	mode := delayFingerprintMode(strings.ToLower(strings.TrimSpace(
		envOr("DITTOBENCH_RELAY_DELAY_FP_MODE", string(delayFingerprintOff)),
	)))
	if mode != delayFingerprintShadow {
		mode = delayFingerprintOff
	}
	return delayFingerprintConfig{
		mode:  mode,
		minMS: envIntDefault("DITTOBENCH_RELAY_DELAY_FP_MIN_MS", 25),
		maxMS: envIntDefault("DITTOBENCH_RELAY_DELAY_FP_MAX_MS", 250),
	}
}

// delayFingerprintFor derives the injected response delay for one successful
// chat completion: minMS + HMAC-SHA256(key, domain || generation || ordinal)
// mod (maxMS-minMS+1), in milliseconds.
//
// generation is the case window the request was admitted in (0 outside any
// window -- no injection, because there is no case to attribute the evidence
// to) and ordinal is the count of already-delayed requests inside that window,
// so concurrent calls within one case each receive their own scheduled value.
// Both inputs are broker-owned; nothing the harness sends influences the
// schedule.
func delayFingerprintFor(
	key []byte,
	generation uint64,
	ordinal uint64,
	cfg delayFingerprintConfig,
) time.Duration {
	if !cfg.enabled() || len(key) == 0 || generation == 0 {
		return 0
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(delayFingerprintDomain))
	var buf [8]byte
	binary.BigEndian.PutUint64(buf[:], generation)
	_, _ = mac.Write(buf[:])
	binary.BigEndian.PutUint64(buf[:], ordinal)
	_, _ = mac.Write(buf[:])
	digest := mac.Sum(nil)
	span := uint64(cfg.maxMS-cfg.minMS) + 1
	ms := uint64(cfg.minMS) + binary.BigEndian.Uint64(digest[:8])%span
	return time.Duration(ms) * time.Millisecond
}
