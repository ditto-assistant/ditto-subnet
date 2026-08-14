// Command generate-confirmation-ablation freezes the public ordinary-v9 case
// population used by the Bench v9 confirmation ablations.  The output contains
// no submission, provider, or operator secrets: it is deterministic trusted-side
// grading data derived entirely from the public datagen contract.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const (
	benchVersion = protocol.BenchVersionV9
	datasetSeed  = int64(20260813)
	caseCount    = 24
	revision     = "dittobench-v9-confirmation-ablation-2026-08-13"
	systemPrompt = "You are Ditto, a helpful assistant with access to tools. Call a tool only when it is the right action for the user's request."
)

type frozenDataset struct {
	SchemaVersion int          `json:"schema_version"`
	Revision      string       `json:"revision"`
	Cases         []frozenCase `json:"cases"`
}

type frozenCase struct {
	CaseID            string                  `json:"case_id"`
	UserID            string                  `json:"user_id"`
	SystemPrompt      string                  `json:"system_prompt"`
	Question          string                  `json:"question"`
	QuestionType      string                  `json:"question_type"`
	ExpectedAnswer    string                  `json:"expected_answer"`
	AnswerKind        string                  `json:"answer_kind,omitempty"`
	AcceptAny         []string                `json:"accept_any,omitempty"`
	DistractorAnswers []string                `json:"distractor_answers,omitempty"`
	ForbiddenAnswer   string                  `json:"forbidden_answer,omitempty"`
	SeedBatches       [][]protocol.MemoryPair `json:"seed_batches"`
}

func normalizedUserID(value string) string {
	if strings.TrimSpace(value) == "" {
		return "miner"
	}
	return value
}

func eligible(item gen.ArtifactCase) bool {
	if strings.TrimSpace(item.ID) == "" || strings.TrimSpace(item.Question) == "" ||
		strings.TrimSpace(item.QuestionType) == "" || strings.TrimSpace(item.ExpectedAnswer) == "" {
		return false
	}
	// The frozen confirmation wire deliberately carries only scalar grading
	// fields.  List cases need AnswerItems and therefore stay outside this first
	// bounded shadow population instead of being lossy-converted.
	if len(item.AnswerItems) != 0 || len(item.AnswerItemKinds) != 0 || len(item.AnswerItemAcceptAny) != 0 {
		return false
	}
	return normalizedUserID(item.UserID) == "miner" &&
		item.AnswerKind != protocol.AnswerList && item.AnswerKind != protocol.AnswerOrderedList
}

func seedBatches(artifact gen.DatasetArtifact, item gen.ArtifactCase) ([][]protocol.MemoryPair, error) {
	userID := normalizedUserID(item.UserID)
	batches := make([][]protocol.MemoryPair, 0, item.RunAfterWave+2)
	seen := make(map[string]protocol.MemoryPair)
	appendBatch := func(source []protocol.MemoryPair) error {
		batch := make([]protocol.MemoryPair, 0, len(source))
		for _, pair := range source {
			if previous, exists := seen[pair.PairID]; exists {
				if previous != pair {
					return fmt.Errorf("pair %s has conflicting payloads", pair.PairID)
				}
				continue
			}
			seen[pair.PairID] = pair
			batch = append(batch, pair)
		}
		if len(batch) != 0 {
			batches = append(batches, batch)
		}
		return nil
	}
	// Bench v9's coherent primary world is carried by the ordinary tool
	// prerequisites and remains in the harness store for the later memory
	// phase.  A fresh ablation container therefore needs that same world before
	// its memory question; MemoryWaves alone are intentionally empty for the
	// primary graph in v9.
	var prerequisites []protocol.MemoryPair
	for _, toolCase := range artifact.ToolCases {
		prerequisites = append(prerequisites, toolCase.PrerequisitePairs...)
	}
	if userID == "miner" {
		if err := appendBatch(prerequisites); err != nil {
			return nil, err
		}
	}
	for _, wave := range artifact.MemoryWaves {
		if normalizedUserID(wave.UserID) != userID || wave.Wave > item.RunAfterWave || len(wave.Pairs) == 0 {
			continue
		}
		if err := appendBatch(wave.Pairs); err != nil {
			return nil, err
		}
	}
	if len(batches) == 0 {
		return nil, fmt.Errorf("case %s has no matching seed batches", item.ID)
	}
	return batches, nil
}

func render() ([]byte, error) {
	profile, ok := gen.ProfileForVersion("full", benchVersion)
	if !ok {
		return nil, errors.New("canonical v9 full profile is unavailable")
	}
	artifact, err := gen.GenerateDataset(datasetSeed, profile, benchVersion)
	if err != nil {
		return nil, fmt.Errorf("generate canonical v9 dataset: %w", err)
	}
	candidates := append([]gen.ArtifactCase(nil), artifact.MemoryCases...)
	sort.Slice(candidates, func(i, j int) bool { return candidates[i].ID < candidates[j].ID })
	result := frozenDataset{SchemaVersion: 1, Revision: revision}
	for _, item := range candidates {
		if !eligible(item) {
			continue
		}
		batches, err := seedBatches(artifact, item)
		if err != nil {
			return nil, err
		}
		result.Cases = append(result.Cases, frozenCase{
			CaseID: item.ID, UserID: normalizedUserID(item.UserID), SystemPrompt: systemPrompt,
			Question: item.Question, QuestionType: item.QuestionType, ExpectedAnswer: item.ExpectedAnswer,
			AnswerKind: item.AnswerKind, AcceptAny: append([]string(nil), item.AcceptAny...),
			DistractorAnswers: append([]string(nil), item.DistractorAnswers...), ForbiddenAnswer: item.ForbiddenAnswer,
			SeedBatches: batches,
		})
		if len(result.Cases) == caseCount {
			break
		}
	}
	if len(result.Cases) != caseCount {
		return nil, fmt.Errorf("wanted %d eligible cases, found %d", caseCount, len(result.Cases))
	}
	raw, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("encode frozen dataset: %w", err)
	}
	return append(raw, '\n'), nil
}

func main() {
	output := flag.String("output", "", "path to the frozen JSON output")
	check := flag.Bool("check", false, "verify that output already has the exact generated bytes")
	flag.Parse()
	if strings.TrimSpace(*output) == "" {
		fmt.Fprintln(os.Stderr, "-output is required")
		os.Exit(2)
	}
	raw, err := render()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if *check {
		existing, readErr := os.ReadFile(*output)
		if readErr != nil || !bytes.Equal(existing, raw) {
			fmt.Fprintln(os.Stderr, "frozen confirmation ablation dataset is out of date")
			os.Exit(1)
		}
		return
	}
	if err := os.WriteFile(*output, raw, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
