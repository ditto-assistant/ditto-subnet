// Command parity-smoke prints a deterministic JSON envelope describing the
// outputs of the three antigaming primitives (PartitionFixture,
// ParaphraseSeed, DistractorBundleFor) for a fixed input set.
//
// CI runs this binary alongside its Python twin
// (ditto/bench/runner/_parity_smoke.py) and diffs the JSON outputs. Any
// drift between the two implementations breaks the build before it can
// reach a validator in production.
//
// Inputs are inlined so the smoke is hermetic: no fixtures, no env, no
// flags. To extend coverage, add a new section to ParitySmoke and mirror
// it in the Python script.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// caseIDs is the deterministic input set shared with the Python twin.
var caseIDs = []string{
	"a", "b", "c", "d", "e", "f", "g", "h", "i", "j",
	"k", "l", "m", "n", "o", "p", "q", "r", "s", "t",
}

// candidates is the fixed pool the distractor sampler draws from.
var candidates = []string{
	"pair-01", "pair-02", "pair-03", "pair-04", "pair-05",
	"pair-06", "pair-07", "pair-08", "pair-09", "pair-10",
	"pair-11", "pair-12", "pair-13", "pair-14", "pair-15",
	"pair-16", "pair-17", "pair-18", "pair-19", "pair-20",
}

const (
	parititonSecret    = "parity-smoke"
	paraphraseSecret   = "parity-smoke"
	distractorSecret   = "parity-smoke"
	distractorCase     = "case-1"
	distractorBundleN  = 5
	partitionPrivFrac  = 0.25
	partitionCanryFrac = 0.15
)

// ParitySmoke is the JSON shape both implementations must produce identically.
type ParitySmoke struct {
	PartitionFixture struct {
		Public  []string `json:"public"`
		Private []string `json:"private"`
		Canary  []string `json:"canary"`
	} `json:"partition_fixture"`
	ParaphraseSeed map[string]string `json:"paraphrase_seed"`
	Distractor     []string          `json:"distractor_bundle"`
}

func main() {
	out := ParitySmoke{}

	hidden := bittensor.PartitionFixture(caseIDs, parititonSecret, partitionPrivFrac, partitionCanryFrac)
	out.PartitionFixture.Public = nilToEmpty(hidden.Public)
	out.PartitionFixture.Private = nilToEmpty(hidden.Private)
	out.PartitionFixture.Canary = nilToEmpty(hidden.Canary)

	out.ParaphraseSeed = make(map[string]string, len(caseIDs))
	for _, id := range caseIDs {
		out.ParaphraseSeed[id] = bittensor.ParaphraseSeed(paraphraseSecret, id)
	}

	expected := []string{"pair-01", "pair-02"}
	forbidden := []string{"pair-19"}
	out.Distractor = nilToEmpty(bittensor.DistractorBundleFor(
		distractorCase, expected, forbidden, candidates, distractorSecret, distractorBundleN,
	))

	buf, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "parity-smoke: %v\n", err)
		os.Exit(2)
	}
	os.Stdout.Write(buf)
	os.Stdout.Write([]byte("\n"))
}

// nilToEmpty maps a nil slice to an empty slice so the JSON shape matches
// Python's empty-list serialisation rather than emitting null.
func nilToEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}
