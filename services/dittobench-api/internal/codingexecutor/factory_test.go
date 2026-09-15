package codingexecutor

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
)

func TestDedicatedDockerHostRefusesRootfulAndNonSocketEndpoints(t *testing.T) {
	for _, value := range []string{
		"unix:///run/ditto-coding-executor/docker.sock",
		"unix:///run/user/1000/docker.sock",
	} {
		if !ValidDedicatedDockerHost(value) {
			t.Errorf("endpoint %q was rejected", value)
		}
	}
	for _, value := range []string{
		"",
		"tcp://127.0.0.1:2375",
		"tcp://sandbox-docker:2375",
		"tcp://localhost:2375",
		"unix:///var/run/docker.sock",
		"unix:///run/docker.sock",
		"unix://run/docker-coding.sock",
		"unix:///run/../var/run/docker.sock",
		"unix:///run//coding/docker.sock",
		"unix:///run/coding/docker",
		"unix:///run/coding docker.sock",
		"ssh://coding@host",
		"unix:///" + strings.Repeat("a", 110) + ".sock",
	} {
		if ValidDedicatedDockerHost(value) {
			t.Errorf("endpoint %q was accepted", value)
		}
	}
}

func TestFactoryCarriesTheDedicatedDockerHostIntoEveryExecutor(t *testing.T) {
	config := validFactoryConfig()
	config.DockerHost = "tcp://127.0.0.1:2375"
	if _, err := NewPhaseFactory(config); err == nil {
		t.Fatal("factory accepted the rootful sandbox daemon endpoint")
	}
	config.DockerHost = "unix:///run/ditto-coding-executor/docker.sock"
	factory, err := NewPhaseFactory(config)
	if err != nil {
		t.Fatal(err)
	}
	executorConfig := factory.executorConfig(codinggrader.Manifest{}, true)
	if executorConfig.DockerHost != config.DockerHost {
		t.Fatalf("executor docker host=%q", executorConfig.DockerHost)
	}
}

func TestExecDockerSelectsOnlyTheConfiguredEndpoint(t *testing.T) {
	directory := t.TempDir()
	script := "#!/bin/sh\nprintf '%s' \"$DOCKER_HOST\"\n"
	if err := os.WriteFile(filepath.Join(directory, "docker"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", directory)
	t.Setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
	inherited, err := execDocker{}.Output(t.Context(), "info")
	if err != nil || string(inherited) != "tcp://127.0.0.1:2375" {
		t.Fatalf("inherited host=%q err=%v", inherited, err)
	}
	dedicated, err := execDocker{host: "unix:///run/ditto-coding-executor/docker.sock"}.Output(t.Context(), "info")
	if err != nil || string(dedicated) != "unix:///run/ditto-coding-executor/docker.sock" {
		t.Fatalf("dedicated host=%q err=%v", dedicated, err)
	}
}

func TestExecDockerDedicatedEndpointDropsInheritedSelectorsAndProxies(t *testing.T) {
	directory := t.TempDir()
	script := "#!/bin/sh\nexec /usr/bin/env\n"
	if err := os.WriteFile(filepath.Join(directory, "docker"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", directory)
	for name, value := range map[string]string{
		"DOCKER_HOST": "tcp://127.0.0.1:2375", "DOCKER_CONTEXT": "shared", "DOCKER_TLS_VERIFY": "1",
		"DOCKER_CONFIG": "/shared/config", "HTTPS_PROXY": "http://172.30.0.2:3128", "no_proxy": "*",
		"all_proxy": "socks5://172.30.0.2:1080", "http_proxy": "http://172.30.0.2:3128",
	} {
		t.Setenv(name, value)
	}
	out, err := execDocker{host: "unix:///run/ditto-coding-executor/docker.sock"}.Output(t.Context(), "info")
	if err != nil {
		t.Fatal(err)
	}
	var selected []string
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		upper := strings.ToUpper(line)
		if strings.HasPrefix(upper, "DOCKER_") || strings.Contains(upper, "_PROXY=") {
			selected = append(selected, line)
		}
	}
	if len(selected) != 1 || selected[0] != "DOCKER_HOST=unix:///run/ditto-coding-executor/docker.sock" {
		t.Fatalf("dedicated executor docker environment selectors = %q", selected)
	}
}

func TestCertificationReadinessRequiresRootlessIsolatedDaemonAndImage(t *testing.T) {
	digest := "sha256:" + strings.Repeat("2", 64)
	factory, err := NewPhaseFactory(validFactoryConfig())
	if err != nil {
		t.Fatal(err)
	}
	fresh := func() *fakeDocker {
		docker := newFakeDocker(Config{Manifest: codinggrader.Manifest{GraderImageDigest: digest}})
		return docker
	}
	if daemon, image := factory.certificationReadiness(t.Context(), fresh(), digest); !daemon || !image {
		t.Fatalf("ready daemon=%v image=%v", daemon, image)
	}
	rootful := fresh()
	rootful.security = []string{"name=seccomp,profile=default"}
	if daemon, image := factory.certificationReadiness(t.Context(), rootful, digest); daemon || image {
		t.Fatalf("rootful daemon=%v image=%v", daemon, image)
	}
	unlabelled := fresh()
	unlabelled.labels = map[string]string{}
	if daemon, image := factory.certificationReadiness(t.Context(), unlabelled, digest); daemon || image {
		t.Fatalf("unlabelled daemon=%v image=%v", daemon, image)
	}
	missing := fresh()
	missing.image.RepoDigests = []string{"registry.invalid/other@sha256:" + strings.Repeat("3", 64)}
	if daemon, image := factory.certificationReadiness(t.Context(), missing, digest); !daemon || image {
		t.Fatalf("missing image daemon=%v image=%v", daemon, image)
	}
	fixture := fresh()
	fixture.image.Config.Labels["io.heyditto.dittobench.coding-supervisor-fixture"] = "true"
	if daemon, image := factory.certificationReadiness(t.Context(), fixture, digest); !daemon || image {
		t.Fatalf("fixture image daemon=%v image=%v", daemon, image)
	}
	if daemon, image := factory.certificationReadiness(t.Context(), fresh(), "latest"); daemon || image {
		t.Fatalf("invalid digest daemon=%v image=%v", daemon, image)
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	if daemon, image := factory.certificationReadiness(cancelled, fresh(), digest); daemon || image {
		t.Fatalf("cancelled daemon=%v image=%v", daemon, image)
	}
	inspected := fresh()
	factory.certificationReadiness(t.Context(), inspected, digest)
	if slices.ContainsFunc(inspected.runs, func(args []string) bool { return len(args) > 0 }) {
		t.Fatal("readiness created a container")
	}
}

func validFactoryConfig() FactoryConfig {
	return FactoryConfig{
		ImageRepository: "registry.invalid/dittobench-coding-supervisor",
		CandidateUID:    65532, CandidateGID: 65532,
		RequireRootless: true, RequireIsolatedDaemon: true,
	}
}
