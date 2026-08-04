package gen

import (
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/textnoise"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// applyV8MemoryWritingNoise changes only user-authored surfaces. Answer keys,
// accepted answers, distractor guards, tool identities, subjects, and agent
// responses remain canonical. Selection is exact per semantic domain and
// identity, so every full dataset exercises personal, business, travel, mixed
// story, integrity, and general writing without exposing which cases were
// selected as protocol metadata.
func applyV8MemoryWritingNoise(seed int64, cases []StagedCase, waves []protocol.SeedRequest) (map[string]int, map[string]int) {
	questionCoverage := map[string]int{}
	pairCoverage := map[string]int{}
	questionIDs := map[string][]string{}
	for _, staged := range cases {
		domain := memoryWritingDomain(staged.Case.QuestionType)
		questionIDs[domain] = append(questionIDs[domain], staged.Case.ID)
	}
	selectedQuestions := map[string]bool{}
	for domain, ids := range questionIDs {
		for id := range textnoise.Select(seed, "memory-question:"+domain, ids, 2_500) {
			selectedQuestions[id] = true
		}
	}
	for i := range cases {
		if !selectedQuestions[cases[i].Case.ID] {
			continue
		}
		projected, stats := textnoise.Project(cases[i].Case.Question, seed, "memory-question:"+cases[i].Case.ID, textnoise.Options{
			Grammar: true, MaxEdits: 1, Protected: memoryCaseProtected(cases[i].Case),
		})
		if stats.Total() > 0 {
			cases[i].Case.Question = projected
			questionCoverage[memoryWritingDomain(cases[i].Case.QuestionType)]++
		}
	}

	var protected []string
	for _, staged := range cases {
		protected = append(protected, memoryCaseProtected(staged.Case)...)
	}
	pairIDs := map[string][]string{}
	for _, wave := range waves {
		for _, pair := range wave.Pairs {
			domain := memoryPairWritingDomain(pair.SessionID)
			pairIDs[domain] = append(pairIDs[domain], pair.PairID)
		}
	}
	selectedPairs := map[string]bool{}
	for domain, ids := range pairIDs {
		for id := range textnoise.Select(seed, "memory-pair:"+domain, ids, 2_500) {
			selectedPairs[id] = true
		}
	}
	for i := range waves {
		for j := range waves[i].Pairs {
			pair := &waves[i].Pairs[j]
			if !selectedPairs[pair.PairID] {
				continue
			}
			projected, stats := textnoise.Project(pair.Prompt, seed, "memory-pair:"+pair.PairID, textnoise.Options{
				Grammar: true, MaxEdits: 1, Protected: protected,
			})
			if stats.Total() > 0 {
				pair.Prompt = projected
				pairCoverage[memoryPairWritingDomain(pair.SessionID)]++
			}
		}
	}
	return questionCoverage, pairCoverage
}

func memoryCaseProtected(c protocol.MemoryCase) []string {
	out := []string{c.ExpectedAnswer, c.ForbiddenAnswer}
	out = append(out, c.AcceptAny...)
	out = append(out, c.AnswerItems...)
	out = append(out, c.DistractorAnswers...)
	out = append(out, c.DumpGuard...)
	out = append(out, c.WritingProtected...)
	return out
}

func memoryWritingDomain(questionType string) string {
	switch {
	case strings.HasPrefix(questionType, "world-story-"):
		return "mixed-story"
	case strings.HasPrefix(questionType, "world-project-"):
		return "business"
	case strings.HasPrefix(questionType, "world-contact-"):
		return "personal"
	case strings.HasPrefix(questionType, "world-trip-"):
		return "travel"
	case strings.Contains(questionType, "injection"), strings.Contains(questionType, "canary"), strings.Contains(questionType, "isolation"), strings.Contains(questionType, "lifecycle"):
		return "integrity"
	default:
		return "general"
	}
}

func memoryPairWritingDomain(sessionID string) string {
	switch {
	case strings.HasPrefix(sessionID, "people-"), strings.Contains(sessionID, "personal"):
		return "personal"
	case strings.HasPrefix(sessionID, "project-"), strings.Contains(sessionID, "business"):
		return "business"
	case strings.HasPrefix(sessionID, "trip-"):
		return "travel"
	default:
		return "general"
	}
}
