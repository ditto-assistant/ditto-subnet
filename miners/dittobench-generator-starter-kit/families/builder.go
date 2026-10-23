// Package families holds the case families the starter generator composes.
//
// A family is a deterministic function that seeds a few memory pairs and
// returns one challenge case grounded in them. The Builder owns the haystack
// (waves, pair IDs, timestamps) so families only describe content.
//
// Every case a family emits must pass the pack contract: its gold answer
// appears in the named evidence pairs, not in the question, and the haystack
// holds at least one decoy value so an agent that dumps all of memory fails.
//
// All randomness flows from the request seed through Builder.Rng; nothing
// reads the clock or the environment, so a request always yields the same
// bytes (the scorer re-runs a seed and rejects a generator that drifts).
package families

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Waves is the number of seeding waves the builder lays out. Later waves can
// supersede earlier facts, which is what makes update chains hard.
const Waves = 3

// Builder accumulates a pack's haystack and cases.
type Builder struct {
	Rng    *rand.Rand
	waves  []protocol.SeedRequest
	cases  []challenge.Case
	nPairs int
	used   map[string]bool
	epoch  time.Time
}

// NewBuilder returns an empty builder for one request.
func NewBuilder(req challenge.GenerateRequest) *Builder {
	b := &Builder{
		Rng:   req.Rand(),
		waves: make([]protocol.SeedRequest, Waves),
		used:  map[string]bool{},
		epoch: time.Date(2026, time.March, 2, 8, 0, 0, 0, time.UTC),
	}
	for i := range b.waves {
		b.waves[i].Wave = i
	}
	return b
}

// Say seeds one conversation pair in a wave and returns its pair ID.
func (b *Builder) Say(wave int, prompt, response string) string {
	b.nPairs++
	id := fmt.Sprintf("p%04d", b.nPairs)
	b.waves[wave].Pairs = append(b.waves[wave].Pairs, protocol.MemoryPair{
		PairID:    id,
		SessionID: fmt.Sprintf("s%d-%d", wave, b.nPairs%5),
		Timestamp: b.epoch.Add(time.Duration(wave*72+b.nPairs) * time.Hour).Format(time.RFC3339),
		Prompt:    prompt,
		Response:  response,
	})
	return id
}

// Add appends a case. Cases run after the last wave so every fact is seeded.
func (b *Builder) Add(c challenge.Case) {
	c.RunAfterWave = Waves - 1
	b.cases = append(b.cases, c)
}

// Pick draws n distinct values from pool that no earlier family has used, so
// two families never collide on a value (which would make one family's gold
// another's distractor).
func (b *Builder) Pick(pool []string, n int) []string {
	var out []string
	for _, i := range b.Rng.Perm(len(pool)) {
		if len(out) == n {
			break
		}
		if v := pool[i]; !b.used[v] {
			b.used[v] = true
			out = append(out, v)
		}
	}
	if len(out) < n {
		panic(fmt.Sprintf("value pool exhausted: need %d more distinct values", n-len(out)))
	}
	return out
}

// Len is the number of cases added so far.
func (b *Builder) Len() int { return len(b.cases) }

// Pack assembles the challenge pack bound to the request.
func (b *Builder) Pack(req challenge.GenerateRequest, name, version string) challenge.Pack {
	return challenge.Pack{
		Contract:            challenge.ContractV1,
		GradingBenchVersion: req.GradingBenchVersion,
		MinerHotkey:         req.MinerHotkey,
		Generator:           challenge.Generator{Name: name, Version: version, Seed: req.Seed},
		MemoryWaves:         b.waves,
		Cases:               b.cases,
	}
}
