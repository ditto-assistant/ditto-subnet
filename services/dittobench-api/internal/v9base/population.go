package v9base

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const modelRoutePreflightCaseID = "__dittobench_model_route_preflight__"

// AggregateModelTelemetry is the trusted run-level broker accounting available
// today. DistinctCaseAttributionComplete deliberately defaults false: aggregate
// request counts cannot prove how many different cases reached inference.
type AggregateModelTelemetry struct {
	ObservedRequests                uint64
	SuccessfulRequests              uint64
	PromptTokens                    uint64
	CompletionTokens                uint64
	TelemetryComplete               bool
	DistinctCaseAttributionComplete bool
	SuccessfulDistinctCases         int
}

// BuildGateEvidence converts trusted ordinary-run observations into the pure
// scoregates contract. It fails closed if aggregate telemetry itself is
// incomplete; lack of distinct-case attribution is instead published as
// insufficient evidence with semantic factor zero in the shadow report.
func BuildGateEvidence(perCase []protocol.CaseScore, model AggregateModelTelemetry, toolTelemetryComplete bool) (scoregates.Evidence, error) {
	modelInput := scoregates.ModelUseInput{
		AdministeredCases: len(perCase),
		ObservedRequests:  model.ObservedRequests, SuccessfulRequests: model.SuccessfulRequests,
		PromptTokens: model.PromptTokens, CompletionTokens: model.CompletionTokens,
		TelemetryComplete:        model.TelemetryComplete,
		CaseAttributionComplete:  model.DistinctCaseAttributionComplete,
		SuccessfulInferenceCases: model.SuccessfulDistinctCases,
	}
	toolInput := scoregates.AuthoritativeToolInput{TelemetryComplete: toolTelemetryComplete}

	for _, item := range perCase {
		switch {
		case isPreflight(item.CaseID):
			modelInput.Excluded.Preflight++
			continue
		case isAblation(item.CaseID):
			modelInput.Excluded.Ablation++
			continue
		case item.ValidatorFault:
			modelInput.Excluded.ValidatorFault++
			continue
		case item.Undelivered:
			modelInput.Excluded.Undelivered++
			continue
		}
		modelInput.EligibleCases++
		// Called is authoritative only when the scorer marked the trajectory as
		// observed through tool_endpoint. An exact harness self-report cannot
		// create matched or unexpected validator-observed executions.
		authoritativeCalled := item.Called
		if !item.Observed {
			authoritativeCalled = nil
		}
		expected, matched, unexpected := observedToolCounts(item.Expected, authoritativeCalled)
		toolInput.ExpectedExecutions += expected
		toolInput.MatchedExecutions += matched
		toolInput.UnexpectedExecutions += unexpected
	}

	if model.DistinctCaseAttributionComplete && model.SuccessfulDistinctCases > modelInput.EligibleCases {
		return scoregates.Evidence{}, fmt.Errorf("successful distinct inference cases exceed eligible population")
	}
	return scoregates.Build(modelInput, toolInput, ContractThresholds(), scoregates.RolloutShadow)
}

func observedToolCounts(expected, called []string) (expectedN, matched, unexpected int) {
	remaining := make(map[string]int, len(expected))
	for _, name := range expected {
		remaining[name]++
	}
	for _, name := range called {
		if remaining[name] > 0 {
			remaining[name]--
			matched++
		} else {
			unexpected++
		}
	}
	return len(expected), matched, unexpected
}

func isPreflight(caseID string) bool {
	return strings.HasPrefix(caseID, "preflight:") || caseID == modelRoutePreflightCaseID
}

func isAblation(caseID string) bool {
	return strings.HasPrefix(caseID, "ablation:") || strings.HasPrefix(caseID, "__dittobench_ablation_")
}
