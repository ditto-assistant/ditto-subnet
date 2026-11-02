package main

import (
	"fmt"
	"testing"
)

func TestScoredCaseOptionsV13ToolAndMemoryParity(t *testing.T) {
	const gateway = "http://cap.host.docker.internal:11436/v1/inference"
	for _, version := range []int{2, 8, 9, 10, 11, 12, 13} {
		for _, caseID := range []string{"tool/case 1", "memory/case 2"} {
			t.Run(fmt.Sprintf("v%d/%s", version, caseID), func(t *testing.T) {
				got := scoredCaseOptions(version, gateway, caseID, "http://tools/case", "user-cap")
				if got.BenchVersion != version || got.ToolEndpoint != "http://tools/case" || got.UserID != "user-cap" {
					t.Fatalf("case identity changed: %+v", got)
				}
				want := ""
				if version == 13 {
					want = v13CaseInferenceBaseURL(version, gateway, caseID)
					if want == "" {
						t.Fatal("missing V13 case route")
					}
				}
				if got.InferenceBaseURL != want {
					t.Fatalf("inference route = %q, want %q", got.InferenceBaseURL, want)
				}
			})
		}
	}
	if scoredCaseOptions(13, "", "tool", "endpoint", "user").InferenceBaseURL != "" {
		t.Fatal("sessionless case invented a broker route")
	}
}
