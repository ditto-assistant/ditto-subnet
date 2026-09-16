package universe

import (
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Story v2 arc-recovery ceiling (issue #1839, report-only until the surface
// pass decision in #1832 lands).
//
// The typed event catalog and its frames are public, so a generator-inverse
// harness (GIH) that knows every slot vocabulary can try to recover an arc's
// graded state straight from the raw pre-pass prose. This probe is that
// adversary in its cheapest form — a bag-of-candidates scan over the arc's own
// memories, given the correct arc — and reports how often it recovers each
// oracle. It is the CEILING the private surface pass must lower and the
// number cmd/parserprobe (#1829) compares against once it exists; it is not a
// gate here, because 8 public frames x ~90 renders per seed can never make raw
// frames unrecoverable (see the story_events.go design note).
//
// The probe MUST stay deterministic so the ceiling is a reproducible artifact
// of the seed set, and it must never exceed the honest-oracle answer rate.

type storyProbeReport struct {
	Arcs     int
	Owner    int
	Status   int
	Sequence int
	NextWho  int
	Quantity int
}

func (r storyProbeReport) String() string {
	pct := func(n int) string { return fmt.Sprintf("%d/%d (%.0f%%)", n, r.Arcs, 100*float64(n)/float64(r.Arcs)) }
	return fmt.Sprintf("owner=%s status=%s sequence=%s next-who=%s quantity=%s", pct(r.Owner), pct(r.Status), pct(r.Sequence), pct(r.NextWho), pct(r.Quantity))
}

// storyGIHProbe recovers arc state by scanning the arc's memories in
// timestamp order with the public vocabularies: the last person name that
// appears after an ownership cue is the owner, the last status surface term
// is the status, provider names in first-appearance order are the sequence.
func storyGIHProbe(w World) storyProbeReport {
	pairs := storyPairMap(w)
	people := make([]string, 0, len(w.People))
	for _, p := range w.People {
		people = append(people, p.Name)
	}
	providers := []string{}
	for _, arc := range w.StoryArcs {
		providers = append(providers, arc.V2.Sequence[0], arc.V2.Sequence[1])
	}
	statusTerms := []string{}
	for _, status := range storyStatusOrder {
		statusTerms = append(statusTerms, storyStatusVocabulary[status]...)
	}
	report := storyProbeReport{Arcs: len(w.StoryArcs)}
	for _, arc := range w.StoryArcs {
		v2 := arc.V2
		memories := make([]protocol.MemoryPair, 0, len(v2.PairIDs))
		for _, id := range v2.PairIDs {
			memories = append(memories, pairs[id])
		}
		sort.Slice(memories, func(i, j int) bool { return memories[i].Timestamp < memories[j].Timestamp })
		var lastOwner, lastStatus, lastNextWho string
		seenProvider := []string{}
		for _, memory := range memories {
			text := strings.ToLower(memory.Prompt)
			for _, sentence := range strings.Split(text, ".") {
				ownerCue := strings.Contains(sentence, "owns") || strings.Contains(sentence, "handed") || strings.Contains(sentence, "owner") || strings.Contains(sentence, "picked up") || strings.Contains(sentence, "passed")
				nextCue := strings.Contains(sentence, "next") || strings.Contains(sentence, "will") || strings.Contains(sentence, "going to") || strings.Contains(sentence, "waiting on")
				for _, name := range people {
					if strings.Contains(sentence, strings.ToLower(name)) {
						if ownerCue {
							lastOwner = name
						}
						if nextCue {
							lastNextWho = name
						}
					}
				}
				for _, term := range statusTerms {
					if strings.Contains(sentence, " "+term) || strings.HasSuffix(sentence, term) {
						lastStatus = term
					}
				}
			}
			for _, provider := range providers {
				if strings.Contains(memory.Prompt, provider) && !contains(seenProvider, provider) {
					seenProvider = append(seenProvider, provider)
				}
			}
		}
		if lastOwner == w.People[v2.Owner].Name {
			report.Owner++
		}
		// A last-status guess cannot answer a records-disagree oracle.
		if !v2.Disagree && contains(storyStatusVocabulary[v2.Status], lastStatus) {
			report.Status++
		}
		if len(seenProvider) >= 2 && seenProvider[0] == v2.Sequence[0] && seenProvider[1] == v2.Sequence[1] {
			report.Sequence++
		}
		if lastNextWho == w.People[v2.Next.Who].Name {
			report.NextWho++
		}
		if v2.Quantity != nil {
			// A quantity needs the record-stated operation, which the scan does
			// not model; count only the trivial replace case where the answer is
			// planted verbatim.
			if v2.Quantity.Op == "replace" {
				report.Quantity++
			}
		}
	}
	return report
}

// This is a diagnostic baseline, not the private-surface acceptance ceiling.
// The assembled public-corpus/calendar contract changes the pre-integration
// 40/17/104/89/20 observation to 45/20/104/90/17. Pin BOTH directions: losing
// parser coverage must not masquerade as hardening. Private-surface acceptance
// still requires the independent parserprobe qualification against the honest
// reference score; this toy scan cannot qualify a release.
var storyProbeRawBaseline = storyProbeReport{Arcs: 104, Owner: 45, Status: 20, Sequence: 104, NextWho: 90, Quantity: 17}

func TestStoryV2ArcRecoveryCeilingIsReportedAndDeterministic(t *testing.T) {
	total := storyProbeReport{}
	for seed := int64(1); seed <= 8; seed++ {
		w := v13World(seed)
		first := storyGIHProbe(w)
		second := storyGIHProbe(v13World(seed))
		if first != second {
			t.Fatalf("seed %d probe is not deterministic: %+v vs %+v", seed, first, second)
		}
		if first.Arcs != 13 {
			t.Fatalf("seed %d probe saw %d arcs", seed, first.Arcs)
		}
		total.Arcs += first.Arcs
		total.Owner += first.Owner
		total.Status += first.Status
		total.Sequence += first.Sequence
		total.NextWho += first.NextWho
		total.Quantity += first.Quantity
	}
	// Report-only: the published ceiling for the raw pre-pass prose. The
	// surface-pass PR is expected to lower it; parserprobe replaces this scan.
	t.Logf("story v2 GIH arc-recovery ceiling on raw pre-pass prose, 8 seeds: %s", total)
	// The honest oracle answers every arc; the scan may never look better than
	// the oracle, which would mean the prose carries a bare planted answer the
	// oracle does not (a generator defect, not adversary strength).
	if total.Owner > total.Arcs || total.Status > total.Arcs || total.Sequence > total.Arcs {
		t.Fatalf("probe exceeded the oracle: %+v", total)
	}
	if total != storyProbeRawBaseline {
		t.Fatalf("raw arc-recovery baseline changed from %s: got %s", storyProbeRawBaseline, total)
	}
}
