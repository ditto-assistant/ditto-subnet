package sandbox

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"
)

func validCodingRuntimeSettings() CodingRuntimeSettings {
	return CodingRuntimeSettings{
		DockerHost:    "unix:///run/ditto-coding-executor/docker.sock",
		EgressNetwork: "ditto-coding-egress",
		EgressProxy:   "http://10.203.0.1:3128",
		HostGatewayIP: "10.203.0.1",
	}
}

// poisonSharedScorerEnvironment sets every ordinary-scorer setting that
// NewLocalDocker reads to a value the coding runtime must never use.
func poisonSharedScorerEnvironment(t *testing.T) {
	t.Helper()
	for name, value := range map[string]string{
		"DITTOBENCH_SANDBOX_MEMORY_LIMIT":           "64g",
		"DITTOBENCH_SANDBOX_TMPFS_LIMIT":            "32g",
		"DITTOBENCH_SANDBOX_START_TIMEOUT_SECONDS":  "9999",
		"DITTOBENCH_SANDBOX_PIDS_LIMIT":             "999999",
		"DITTOBENCH_SANDBOX_HARDEN":                 "0",
		"DITTOBENCH_SANDBOX_SECCOMP_PROFILE":        "shared-seccomp",
		"DITTOBENCH_SANDBOX_APPARMOR_PROFILE":       "shared-apparmor",
		"DITTOBENCH_REQUIRE_ROOTLESS_DOCKER":        "false",
		"DITTOBENCH_REQUIRE_ISOLATED_DOCKER_DAEMON": "false",
		"DITTOBENCH_SANDBOX_HOST_GATEWAY_IP":        "172.17.0.1",
		"DITTOBENCH_SANDBOX_EGRESS_NETWORK":         "ditto-sandbox",
		"DITTOBENCH_SANDBOX_EGRESS_PROXY":           "http://172.30.0.2:3128",
		"DITTOBENCH_OPENROUTER_SHIM_CA_BUNDLE_PATH": "/var/lib/dittobench-openrouter-shim/ca-bundle.pem",
		"GITHUB_TOKEN_FILE":                         "/run/secrets/github-token",
		"DITTOBENCH_ALLOW_PRIVATE_HARNESS":          "true",
		"DOCKER_HOST":                               "tcp://127.0.0.1:2375",
	} {
		t.Setenv(name, value)
	}
}

// The whole struct is compared, so a future field that NewCodingRuntimeDocker
// derives from the process environment fails here.
func TestCodingRuntimeDockerNeverReadsSharedScorerSettings(t *testing.T) {
	poisonSharedScorerEnvironment(t)
	settings := validCodingRuntimeSettings()
	settings.SeccompProfile = "coding-seccomp"
	settings.AppArmorProfile = "coding-apparmor"
	docker, err := NewCodingRuntimeDocker(settings)
	if err != nil {
		t.Fatal(err)
	}
	want := &LocalDocker{
		HarnessPort: "8080", MemoryLimit: "3g", TmpfsLimit: "512m", CPULimit: "2",
		BuildTimeout: 25 * time.Minute, StartTimeout: 120 * time.Second, PidsLimit: 512, Harden: true,
		SeccompProfile: "coding-seccomp", AppArmorProfile: "coding-apparmor",
		RequireRootless: true, RequireIsolatedDaemon: true,
		HostGatewayIP: "10.203.0.1", EgressNetwork: "ditto-coding-egress", EgressProxy: "http://10.203.0.1:3128",
		DockerHost: "unix:///run/ditto-coding-executor/docker.sock",
	}
	if !reflect.DeepEqual(docker, want) {
		t.Fatalf("coding runtime docker = %+v, want %+v", docker, want)
	}
}

func TestCodingRuntimeContainerHasNoCABundleAndOnlyItsOwnProxyAndGateway(t *testing.T) {
	poisonSharedScorerEnvironment(t)
	docker, err := NewCodingRuntimeDocker(validCodingRuntimeSettings())
	if err != nil {
		t.Fatal(err)
	}
	args := docker.runArgs("ditto-screen/agent:latest", map[string]string{})
	joined := strings.Join(args, "\n")
	for _, forbidden := range []string{
		"--mount", OpenRouterShimCABundlePath, "openrouter-shim", "openrouter.ai",
		"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
		"ditto-sandbox", "172.30.0.2", "172.17.0.1", "host-gateway", "shared-seccomp", "shared-apparmor",
	} {
		if strings.Contains(joined, forbidden) {
			t.Fatalf("coding harness arguments contain shared setting %q:\n%s", forbidden, joined)
		}
	}
	for _, pair := range [][2]string{
		{"--network", "ditto-coding-egress"},
		{"--add-host", "host.docker.internal:10.203.0.1"},
		{"-e", "HTTPS_PROXY=http://10.203.0.1:3128"},
		{"-e", "HTTP_PROXY=http://10.203.0.1:3128"},
		{"--memory", "3g"},
		{"--pids-limit", "512"},
	} {
		index := slices.Index(args, pair[0])
		found := false
		for index >= 0 && index+1 < len(args) {
			if args[index+1] == pair[1] {
				found = true
				break
			}
			next := slices.Index(args[index+1:], pair[0])
			if next < 0 {
				break
			}
			index += next + 1
		}
		if !found {
			t.Fatalf("coding harness arguments lack %s %s:\n%s", pair[0], pair[1], joined)
		}
	}
	if !slices.Contains(args, "--cap-drop") {
		t.Fatalf("coding harness is not hardened:\n%s", joined)
	}
}

func TestCodingRuntimeSettingsRefuseMissingOrSharedShapes(t *testing.T) {
	mutations := map[string]func(*CodingRuntimeSettings){
		"no docker host":         func(s *CodingRuntimeSettings) { s.DockerHost = "" },
		"tcp docker host":        func(s *CodingRuntimeSettings) { s.DockerHost = "tcp://127.0.0.1:2375" },
		"relative docker host":   func(s *CodingRuntimeSettings) { s.DockerHost = "unix://run/docker.sock" },
		"unclean docker host":    func(s *CodingRuntimeSettings) { s.DockerHost = "unix:///run/x/../docker.sock" },
		"no network":             func(s *CodingRuntimeSettings) { s.EgressNetwork = "" },
		"network flag":           func(s *CodingRuntimeSettings) { s.EgressNetwork = "-host" },
		"network uppercase":      func(s *CodingRuntimeSettings) { s.EgressNetwork = "Coding" },
		"no proxy":               func(s *CodingRuntimeSettings) { s.EgressProxy = "" },
		"https proxy":            func(s *CodingRuntimeSettings) { s.EgressProxy = "https://10.203.0.1:3128" },
		"proxy hostname":         func(s *CodingRuntimeSettings) { s.EgressProxy = "http://egress-proxy:3128" },
		"proxy public ip":        func(s *CodingRuntimeSettings) { s.EgressProxy = "http://8.8.8.8:3128" },
		"proxy loopback":         func(s *CodingRuntimeSettings) { s.EgressProxy = "http://127.0.0.1:3128" },
		"proxy without port":     func(s *CodingRuntimeSettings) { s.EgressProxy = "http://10.203.0.1" },
		"proxy path":             func(s *CodingRuntimeSettings) { s.EgressProxy = "http://10.203.0.1:3128/" },
		"proxy userinfo":         func(s *CodingRuntimeSettings) { s.EgressProxy = "http://user:pw@10.203.0.1:3128" },
		"proxy query":            func(s *CodingRuntimeSettings) { s.EgressProxy = "http://10.203.0.1:3128?x=1" },
		"proxy leading zero":     func(s *CodingRuntimeSettings) { s.EgressProxy = "http://10.203.0.1:03128" },
		"no gateway":             func(s *CodingRuntimeSettings) { s.HostGatewayIP = "" },
		"gateway loopback":       func(s *CodingRuntimeSettings) { s.HostGatewayIP = "127.0.0.1" },
		"gateway public":         func(s *CodingRuntimeSettings) { s.HostGatewayIP = "8.8.8.8" },
		"gateway ipv6":           func(s *CodingRuntimeSettings) { s.HostGatewayIP = "fd00::1" },
		"gateway magic":          func(s *CodingRuntimeSettings) { s.HostGatewayIP = "host-gateway" },
		"seccomp unconfined":     func(s *CodingRuntimeSettings) { s.SeccompProfile = "unconfined" },
		"apparmor unconfined":    func(s *CodingRuntimeSettings) { s.AppArmorProfile = "Unconfined" },
		"seccomp path separator": func(s *CodingRuntimeSettings) { s.SeccompProfile = "/etc/seccomp.json" },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			settings := validCodingRuntimeSettings()
			mutate(&settings)
			docker, err := NewCodingRuntimeDocker(settings)
			if err == nil || docker != nil {
				t.Fatalf("accepted %+v", settings)
			}
			if strings.Contains(err.Error(), "10.203") || strings.Contains(err.Error(), "unix://") {
				t.Fatalf("refusal echoes a configured value: %v", err)
			}
		})
	}
}

func TestDedicatedDockerEndpointInheritsNoDockerOrProxySelector(t *testing.T) {
	directory := t.TempDir()
	script := "#!/bin/sh\nexec /usr/bin/env\n"
	if err := os.WriteFile(filepath.Join(directory, "docker"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", directory)
	for name, value := range map[string]string{
		"DOCKER_HOST": "tcp://127.0.0.1:2375", "DOCKER_CONTEXT": "shared", "DOCKER_TLS_VERIFY": "1",
		"DOCKER_CERT_PATH": "/shared/certs", "DOCKER_CONFIG": "/shared/config", "DOCKER_API_VERSION": "1.40",
		"HTTPS_PROXY": "http://172.30.0.2:3128", "http_proxy": "http://172.30.0.2:3128",
		"NO_PROXY": "*", "all_proxy": "socks5://172.30.0.2:1080",
	} {
		t.Setenv(name, value)
	}
	docker, err := NewCodingRuntimeDocker(validCodingRuntimeSettings())
	if err != nil {
		t.Fatal(err)
	}
	out, err := docker.dockerOutput(context.Background(), "info")
	if err != nil {
		t.Fatal(err)
	}
	// The oracle is independent of inheritedDockerSelector, so weakening the
	// production rule cannot also weaken this check.
	var selected []string
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		name, _, _ := strings.Cut(line, "=")
		upper := strings.ToUpper(name)
		if strings.HasPrefix(upper, "DOCKER_") || strings.HasSuffix(upper, "_PROXY") {
			selected = append(selected, line)
		}
	}
	if !reflect.DeepEqual(selected, []string{"DOCKER_HOST=unix:///run/ditto-coding-executor/docker.sock"}) {
		t.Fatalf("dedicated docker CLI environment selectors = %q", selected)
	}
}
