package inference

import (
	"net/http"
	"testing"
	"time"
)

// TestBackpressureDelayIsTheLongerOfRetryAfterAndJitter pins the delay rule
// deterministically, which the end-to-end test cannot: a real wait of exactly
// 1s satisfies "at least Retry-After" whether or not the jitter floor is
// applied. Injecting a jitter that returns its full ceiling separates them.
func TestBackpressureDelayIsTheLongerOfRetryAfterAndJitter(t *testing.T) {
	result := &providerHTTPResult{
		status: http.StatusTooManyRequests,
		header: http.Header{"Retry-After": []string{"1"}},
		body:   []byte(receiptFreeEnvelope(429, liveBackpressureModel)),
	}
	ceiling := func(c time.Duration) time.Duration { return c }
	policy := providerRetryPolicy{
		retryBackpressure:        true,
		receiptFreeExpectedModel: liveBackpressureModel,
		retryTransient:           true,
		transientMaxAttempts:     confirmationTransientMaxAttempts,
		jitter:                   ceiling,
	}

	// After the second attempt the jittered ceiling is 4s, above the 1s floor.
	delay, retry := providerRetryDelay(policy, result, 2, confirmationTransientMaxAttempts)
	if !retry || delay != 4*time.Second {
		t.Fatalf("delay = %v retry = %v, want 4s (jitter above a 1s Retry-After)", delay, retry)
	}

	// A long Retry-After still wins: never retry sooner than the provider asked.
	result.header = http.Header{"Retry-After": []string{"30"}}
	delay, retry = providerRetryDelay(policy, result, 2, confirmationTransientMaxAttempts)
	if !retry || delay != 30*time.Second {
		t.Fatalf("delay = %v retry = %v, want 30s (Retry-After above jitter)", delay, retry)
	}

	// Without transient retry activated the legacy rule is untouched.
	policy.retryTransient = false
	result.header = http.Header{"Retry-After": []string{"1"}}
	delay, retry = providerRetryDelay(policy, result, 1, 2)
	if !retry || delay != time.Second {
		t.Fatalf("delay = %v retry = %v, want the bare 1s Retry-After", delay, retry)
	}
}
