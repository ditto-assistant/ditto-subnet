package codingexecutor

import "testing"

func TestPolicyInspectionAcceptsDocker29CapabilityNames(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	// Docker 29 echoes added capabilities with the CAP_ prefix.
	docker.inspectionMutate = func(value *dockerContainerInspection) {
		for index, name := range value.HostConfig.CapAdd {
			value.HostConfig.CapAdd[index] = "CAP_" + name
		}
	}
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := executor.Preflight(t.Context(), config.Manifest.GraderPlanSHA256); err != nil {
		t.Fatalf("CAP_-prefixed capability names were refused: %v", err)
	}
	docker.inspectionMutate = func(value *dockerContainerInspection) {
		value.HostConfig.CapAdd = append(value.HostConfig.CapAdd, "CAP_SYS_ADMIN")
	}
	other, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := other.Preflight(t.Context(), config.Manifest.GraderPlanSHA256); err == nil {
		t.Fatal("an extra CAP_SYS_ADMIN must still be refused")
	}
}
