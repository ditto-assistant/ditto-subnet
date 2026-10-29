package longmemeval

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"
)

// judgeProviderRoutingGolden is the exact provider block the judge lane sends.
// Platform's confirmation proxy test feeds this same file through its real
// request lock, so this golden is the one place the two processes meet.
//
// Why it exists: #3167 added "zdr" to this block on the strength of a test
// comparing it key for key with research/judge_evaluator.py -- a mirror, not
// the receiver. The proxy compared the block for exact equality with a dict
// that had no "zdr", refused every judge request with a 403, and LongMem
// confirmation failed on every bundle from v0.397.0 until the proxy was fixed
// (ditto-subnet#3214). Pinning the bytes here and asserting on them there is
// the test that would have caught it.
const judgeProviderRoutingGolden = "testdata/judge-provider-routing-v1.json"

func TestJudgeProviderRoutingMatchesTheGoldenTheProxyIsTestedAgainst(t *testing.T) {
	got, err := json.Marshal(judgeProviderRouting(officialJudgeRouteProvider))
	if err != nil {
		t.Fatal(err)
	}
	if os.Getenv("UPDATE_JUDGE_ROUTING_GOLDEN") == "1" {
		if err := os.WriteFile(judgeProviderRoutingGolden, append(got, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(judgeProviderRoutingGolden)
	if err != nil {
		t.Fatalf("read %s: %v", judgeProviderRoutingGolden, err)
	}
	if !bytes.Equal(got, bytes.TrimSpace(want)) {
		t.Fatalf(
			"judge provider block drifted from %s.\n got: %s\nwant: %s\n"+
				"Regenerate with UPDATE_JUDGE_ROUTING_GOLDEN=1 and rerun Platform's "+
				"test_scorer_judge_provider_block_passes_the_confirmation_lock: "+
				"the proxy must accept whatever this block becomes.",
			judgeProviderRoutingGolden, got, want,
		)
	}
}
