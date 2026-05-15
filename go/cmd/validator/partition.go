package main

import (
	"math/rand"
	"sort"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// partitionedPlan is the validator's per-tempo case selection: the
// core/retrieval cases the validator will actually score this run,
// stamped with their visibility bucket so the scorer can pass it
// through to Score.Visibility.
type partitionedPlan struct {
	Core      []ToolCallCase
	CoreVis   map[string]string
	Retrieval []RetrievalCase
	RetrVis   map[string]string
}

// planPartitions assigns each loaded case to a visibility bucket using
// the antigaming PartitionFixture, filters by cfg.Mechanism +
// cfg.Visibility, and applies cfg.Sample (deterministic over
// cfg.Secret so the run is reproducible).
func planPartitions(core []ToolCallCase, retrieval []RetrievalCase, cfg Config) partitionedPlan {
	plan := partitionedPlan{
		CoreVis: map[string]string{},
		RetrVis: map[string]string{},
	}

	if cfg.Mechanism == "ditto_core" || cfg.Mechanism == "all" {
		ids := caseIDs(core)
		split := bittensor.PartitionFixture(ids, cfg.Secret, cfg.PrivateFrac, cfg.CanaryFrac)
		vis := visibilityIndex(split)
		plan.CoreVis = vis
		for _, c := range core {
			if !inVisibility(vis[c.ID], cfg.Visibility) {
				continue
			}
			plan.Core = append(plan.Core, c)
		}
		plan.Core = sampleCore(plan.Core, cfg)
	}
	if cfg.Mechanism == "ditto_retrieval" || cfg.Mechanism == "all" {
		ids := retrievalCaseIDs(retrieval)
		split := bittensor.PartitionFixture(ids, cfg.Secret, cfg.PrivateFrac, cfg.CanaryFrac)
		vis := visibilityIndex(split)
		plan.RetrVis = vis
		for _, c := range retrieval {
			if !inVisibility(vis[c.ID], cfg.Visibility) {
				continue
			}
			plan.Retrieval = append(plan.Retrieval, c)
		}
		plan.Retrieval = sampleRetrieval(plan.Retrieval, cfg)
	}
	return plan
}

func caseIDs(cases []ToolCallCase) []string {
	ids := make([]string, len(cases))
	for i, c := range cases {
		ids[i] = c.ID
	}
	return ids
}

func retrievalCaseIDs(cases []RetrievalCase) []string {
	ids := make([]string, len(cases))
	for i, c := range cases {
		ids[i] = c.ID
	}
	return ids
}

// visibilityIndex inverts a HiddenSet into a "case_id -> bucket" map.
// Any case_id absent from the HiddenSet falls into the "public" bucket
// (PartitionFixture never drops cases, but if a caller hand-rolls a
// HiddenSet we still want a defined answer).
func visibilityIndex(h bittensor.HiddenSet) map[string]string {
	out := map[string]string{}
	for _, id := range h.Private {
		out[id] = "private"
	}
	for _, id := range h.Canary {
		out[id] = "canary"
	}
	for _, id := range h.Public {
		out[id] = "public"
	}
	return out
}

func inVisibility(have, want string) bool {
	if want == "all" {
		return true
	}
	if have == "" {
		have = "public"
	}
	return have == want
}

func sampleCore(cases []ToolCallCase, cfg Config) []ToolCallCase {
	if cfg.Sample <= 0 || cfg.Sample >= len(cases) {
		return cases
	}
	rng := rand.New(rand.NewSource(seedHash(cfg.Secret + "|core")))
	sort.Slice(cases, func(i, j int) bool { return cases[i].ID < cases[j].ID })
	rng.Shuffle(len(cases), func(i, j int) { cases[i], cases[j] = cases[j], cases[i] })
	return cases[:cfg.Sample]
}

func sampleRetrieval(cases []RetrievalCase, cfg Config) []RetrievalCase {
	if cfg.Sample <= 0 || cfg.Sample >= len(cases) {
		return cases
	}
	rng := rand.New(rand.NewSource(seedHash(cfg.Secret + "|retrieval")))
	sort.Slice(cases, func(i, j int) bool { return cases[i].ID < cases[j].ID })
	rng.Shuffle(len(cases), func(i, j int) { cases[i], cases[j] = cases[j], cases[i] })
	return cases[:cfg.Sample]
}

// seedHash is a tiny stable string-to-int64 hash so the validator's
// sample selection is reproducible across runs with the same secret
// without pulling in a full hash dependency.
func seedHash(s string) int64 {
	var h int64 = 1469598103934665603
	for _, b := range []byte(s) {
		h ^= int64(b)
		h *= 1099511628211
	}
	if h < 0 {
		h = -h
	}
	return h
}
