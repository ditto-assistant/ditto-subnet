// Package toolprobe measures how well a model-free nearest-neighbor table can
// recover the complete scored tool outcome from prompt surface alone. Merely
// inferring gmail_send from "email" is not a benchmark exploit; predicting the
// seed-bound recipient, mutation target, or served result without resolving the
// generated world is.
package toolprobe

import (
	"fmt"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type FamilyResult struct {
	Correct int
	Total   int
}

func (r FamilyResult) Accuracy() float64 {
	if r.Total == 0 {
		return 0
	}
	return float64(r.Correct) / float64(r.Total)
}

type Result struct {
	Correct  int
	Total    int
	Families map[string]FamilyResult
}

func (r Result) Accuracy() float64 {
	if r.Total == 0 {
		return 0
	}
	return float64(r.Correct) / float64(r.Total)
}

type example struct {
	grams     map[string]struct{}
	signature string
}

// Run trains on sequential seeds [trainStart, trainStart+trainSeeds) and tests
// on the immediately following held-out seeds. Seeds are disjoint by contract.
func Run(benchVersion int, runSize string, trainStart int64, trainSeeds, heldOutSeeds int) (Result, error) {
	if trainSeeds < 1 || heldOutSeeds < 1 {
		return Result{}, fmt.Errorf("train and held-out seed counts must be positive")
	}
	prof, ok := gen.ProfileForVersion(runSize, benchVersion)
	if !ok {
		return Result{}, fmt.Errorf("unsupported run_size %q", runSize)
	}
	var train []example
	for i := 0; i < trainSeeds; i++ {
		artifact, err := gen.GenerateDataset(trainStart+int64(i), prof, benchVersion)
		if err != nil {
			return Result{}, err
		}
		needles := fixtureNeedles(artifact)
		for _, tc := range artifact.ToolCases {
			train = append(train, example{grams: char4grams(tc.Prompt), signature: toolOutcomeSignature(tc, needles[tc.ID])})
		}
	}
	result := Result{Families: map[string]FamilyResult{}}
	for i := 0; i < heldOutSeeds; i++ {
		seed := trainStart + int64(trainSeeds+i)
		artifact, err := gen.GenerateDataset(seed, prof, benchVersion)
		if err != nil {
			return Result{}, err
		}
		needles := fixtureNeedles(artifact)
		for _, tc := range artifact.ToolCases {
			want := toolOutcomeSignature(tc, needles[tc.ID])
			got := nearestSignature(char4grams(tc.Prompt), train)
			family := result.Families[tc.Category]
			family.Total++
			result.Total++
			if got == want {
				family.Correct++
				result.Correct++
			}
			result.Families[tc.Category] = family
		}
	}
	return result, nil
}

func SortedFamilies(result Result) []string {
	names := make([]string, 0, len(result.Families))
	for name := range result.Families {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func toolOutcomeSignature(tc protocol.ToolCase, needle string) string {
	if len(tc.ExpectedTools) == 0 {
		return "<no-tool>"
	}
	tools := make([]string, len(tc.ExpectedTools))
	for i, spec := range tc.ExpectedTools {
		part := spec.Name
		keys := make([]string, 0, len(spec.RequiredArgs))
		for key := range spec.RequiredArgs {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			part += "|" + key + "=" + spec.RequiredArgs[key]
		}
		tools[i] = part
	}
	if tc.FuzzyTrajectory {
		sort.Strings(tools)
	}
	signature := strings.Join(tools, " -> ")
	if needle != "" {
		signature += "|result=" + needle
	}
	return signature
}

func fixtureNeedles(artifact gen.DatasetArtifact) map[string]string {
	out := make(map[string]string, len(artifact.ToolFixtures))
	for _, fixture := range artifact.ToolFixtures {
		out[fixture.CaseID] = fixture.Needle
	}
	return out
}

func char4grams(text string) map[string]struct{} {
	normalized := strings.Join(strings.Fields(strings.ToLower(text)), " ")
	runes := []rune("   " + normalized + "   ")
	grams := make(map[string]struct{}, len(runes))
	for i := 0; i+4 <= len(runes); i++ {
		grams[string(runes[i:i+4])] = struct{}{}
	}
	return grams
}

func nearestSignature(query map[string]struct{}, train []example) string {
	bestScore := -1.0
	best := ""
	for _, candidate := range train {
		intersection := 0
		for gram := range query {
			if _, ok := candidate.grams[gram]; ok {
				intersection++
			}
		}
		union := len(query) + len(candidate.grams) - intersection
		score := 0.0
		if union > 0 {
			score = float64(intersection) / float64(union)
		}
		if score > bestScore {
			bestScore = score
			best = candidate.signature
		}
	}
	return best
}
