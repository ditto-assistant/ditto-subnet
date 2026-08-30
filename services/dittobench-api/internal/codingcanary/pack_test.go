package codingcanary

import (
	"path/filepath"
	"testing"
	"time"
)

func TestLoadPublicPackMatchesThePinnedLeaseIdentity(t *testing.T) {
	pack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	if pack.CanaryManifestSHA256 != "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903" {
		t.Fatalf("canary manifest sha=%s", pack.CanaryManifestSHA256)
	}
	if pack.InferencePolicySHA256 != lockedInferencePolicySHA256 || pack.TaskID != publicCanaryTaskID {
		t.Fatalf("pack identity=%+v", pack)
	}
	if pack.CPUQuotaMillis != 2000 || pack.MemoryLimitBytes != 1024*1024*1024 || pack.PidsLimit != 256 {
		t.Fatalf("resource envelope=%+v", pack)
	}
}

func TestPublicPackExecutionPlansValidate(t *testing.T) {
	pack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC)
	plans, err := pack.executionPlans(
		now, now.Add(20*time.Minute), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
		"sha256:"+repeatHex("2"),
	)
	if err != nil {
		t.Fatal(err)
	}
	if plans.runner.CaseID != publicCanaryTaskID || plans.grader.CaseID != publicCanaryTaskID {
		t.Fatalf("case ids runner=%s grader=%s", plans.runner.CaseID, plans.grader.CaseID)
	}
	if len(plans.visible) == 0 || len(plans.graderBundle) == 0 {
		t.Fatal("execution plans omitted capsule bytes")
	}
}

func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func repeatHex(value string) string {
	out := make([]byte, 0, 64)
	for len(out) < 64 {
		out = append(out, value...)
	}
	return string(out[:64])
}
