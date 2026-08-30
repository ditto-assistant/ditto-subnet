package main

import (
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

func TestCodingShadowHostIsDefaultOffAndLockedPolicyIsCanonical(t *testing.T) {
	t.Setenv("DITTOBENCH_CODING_SHADOW_ENABLED", "false")
	host, err := codingShadowHostFromEnvironment(sandbox.NewLocalDocker(), 8000, 11436)
	if err != nil || host != nil {
		t.Fatalf("default host=%v err=%v", host, err)
	}
	path := filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract",
		"testdata", "coding_inference_policy_locked_v1.json",
	)
	path, err = filepath.Abs(path)
	if err != nil {
		t.Fatal(err)
	}
	policy, err := loadCodingInferencePolicy(path)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil || digest != "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6" {
		t.Fatalf("policy digest=%s err=%v", digest, err)
	}
}
