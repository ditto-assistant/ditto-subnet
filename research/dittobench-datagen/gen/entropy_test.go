package gen

import (
	"fmt"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV5FamilySurfaceEntropy is the anti-overfit / anti-enumeration guard. The v5
// memory-case families must NOT be a small closed set of templates a miner can
// enumerate into a static cue list (the v4 phrase-list exploit, one level up). This
// pins a high distinct-surface count across seeds for each family, so shrinking the
// grammars/pools (regressing toward memorizable templates) fails the build.
//
// The declarative WRITE turn is the highest-risk family (it is the direct analog of
// the v4 durable-write cue list), so it carries the strictest floor.
func TestV5FamilySurfaceEntropy(t *testing.T) {
	// question_type -> set of distinct rendered surfaces across many seeds. For the
	// declarative WRITE (which has no question), collect the seeded /run prompt.
	surfaces := map[string]map[string]struct{}{}
	add := func(kind, s string) {
		s = strings.TrimSpace(s)
		if s == "" {
			return
		}
		if surfaces[kind] == nil {
			surfaces[kind] = map[string]struct{}{}
		}
		surfaces[kind][s] = struct{}{}
	}
	for i := 0; i < 40; i++ {
		seed := int64(i*7919 + 11)
		r, err := NewRNGForVersion(seed, protocol.BenchVersionV5)
		if err != nil {
			t.Fatal(err)
		}
		s, err := GenerateMemorySuiteForVersion(r, seed, 90, 4, 0.2, protocol.BenchVersionV5)
		if err != nil {
			t.Fatal(err)
		}
		for _, sc := range s.Cases {
			add(sc.Case.QuestionType, sc.Case.Question)
		}
		// Declarative-write prompts are the /run write turns; collect their text.
		for _, sc := range s.Cases {
			if sc.Case.QuestionType == QTDeclarativeWrite {
				add(QTDeclarativeWrite, sc.Case.Question)
			}
		}
	}
	// Minimum distinct surfaces per family (across 40 seeds). Floors chosen well
	// above the small-template regime and below the current combinatorial space, so
	// they catch a regression without being brittle to phrasing tweaks.
	floors := map[string]int{
		QTMultiHop:            30,
		QTTemporalDepth:       25,
		QTDeclarativeWrite:    20,
		QTAbstainConfab:       12,
		QTDeclarativeAck:      8,
		QTChitchat:            8,
		QTDeclarativeBehavior: 8,
	}
	for kind, floor := range floors {
		got := len(surfaces[kind])
		if got < floor {
			t.Errorf("%s: only %d distinct surfaces across 40 seeds (want >= %d) — family is too enumerable (overfit risk)", kind, got, floor)
		}
	}
	if !t.Failed() {
		var parts []string
		for kind := range floors {
			parts = append(parts, fmt.Sprintf("%s=%d", kind, len(surfaces[kind])))
		}
		t.Logf("distinct v5 surfaces across 40 seeds: %s", strings.Join(parts, " "))
	}
}
