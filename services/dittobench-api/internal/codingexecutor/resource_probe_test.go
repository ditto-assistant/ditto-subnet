package codingexecutor

import "testing"

func TestInspectRequestedResourceConfigEchoesTheLaunchRequest(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	requested, err := executor.InspectRequestedResourceConfig(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	policy := config.Manifest.ResourcePolicy
	if requested.MemoryLimitBytes != int64(policy.MemoryLimitBytes) || requested.MemorySwapBytes != 0 ||
		requested.NanoCPUs != int64(policy.CPUQuotaMillis)*1_000_000 || requested.PidsLimit != int64(policy.PidsLimit) ||
		!requested.ReadonlyRootfs || requested.ScratchLimitBytes != int64(policy.ScratchLimitBytes) ||
		requested.GraderImageDigest != config.Manifest.GraderImageDigest {
		t.Fatalf("requested=%#v", requested)
	}
	created, started := false, false
	for _, run := range docker.runs {
		if len(run) > 0 && run[0] == "create" {
			created = true
		}
	}
	docker.mu.Lock()
	for _, container := range docker.active {
		started = started || container.started
	}
	remaining := len(docker.active)
	docker.mu.Unlock()
	if !created || started || remaining != 0 {
		t.Fatalf("inspection must create, never start, and remove (created=%v started=%v remaining=%d)", created, started, remaining)
	}
}

func TestInspectRequestedResourceConfigRefusesADriftedContainer(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	docker.inspectionMutate = func(value *dockerContainerInspection) { value.HostConfig.PidsLimit *= 2 }
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := executor.InspectRequestedResourceConfig(t.Context()); err == nil {
		t.Fatal("a container whose configuration drifts from the plan must be refused")
	}
}

func TestInspectRequestedResourceConfigRefusesAnAuthoringOnlyExecutor(t *testing.T) {
	config := testConfig(t)
	config.AuthoringOnly = true
	config.Manifest.GraderPlanSHA256 = ""
	executor, err := newWithDocker(config, newFakeDocker(config))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := executor.InspectRequestedResourceConfig(t.Context()); err == nil {
		t.Fatal("an authoring-only executor must not expose a grading requested-config inspection")
	}
}
