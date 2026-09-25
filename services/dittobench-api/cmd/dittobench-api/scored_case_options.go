package main

import "github.com/ditto-assistant/dittobench-api/internal/runner"

// Tool and memory cases must both receive the same case-scoped inference
// contract. Without it, concurrent V13 tool calls lose exact catalog attribution.
// The existing attribution-only mechanism below V13 remains unchanged.
func scoredCaseOptions(version int, gateway, caseID, toolEndpoint, userID string) runner.CaseOptions {
	return runner.CaseOptions{
		BenchVersion: version, ToolEndpoint: toolEndpoint, UserID: userID,
		InferenceBaseURL: v13CaseInferenceBaseURL(version, gateway, caseID),
	}
}
