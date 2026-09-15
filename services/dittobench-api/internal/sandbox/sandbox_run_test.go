package sandbox

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/netip"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestRunFailureRemovesPartiallyCreatedNamedContainer(t *testing.T) {
	d := NewLocalDocker()
	var runName string
	removed := false
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		switch args[0] {
		case "run":
			for i := 0; i+1 < len(args); i++ {
				if args[i] == "--name" {
					runName = args[i+1]
				}
			}
			return nil, errors.New("start deadline")
		case "rm":
			removed = len(args) == 3 && args[1] == "-f" && args[2] == runName
			return nil, nil
		default:
			t.Fatalf("unexpected docker command: %v", args)
			return nil, nil
		}
	}

	if _, err := d.Run(context.Background(), "operator-image:latest", nil); err == nil {
		t.Fatal("expected docker start failure")
	}
	if runName == "" || !removed {
		t.Fatalf("partial container was not removed by exact generated name %q", runName)
	}
}

func TestStartTimeoutDefaultsToTwoMinutes(t *testing.T) {
	if got := (&LocalDocker{}).startTimeout(); got != 2*time.Minute {
		t.Fatalf("zero-value start timeout = %s", got)
	}
}

func TestAvailableRequiresRootlessWhenConfigured(t *testing.T) {
	d := NewLocalDocker()
	d.RequireRootless = true
	d.HostGatewayIP = "192.0.2.10"
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		if !reflect.DeepEqual(args, []string{"info", "--format", "{{json .SecurityOptions}}"}) {
			t.Fatalf("unexpected Docker probe: %v", args)
		}
		return []byte(`["name=seccomp,profile=builtin","name=cgroupns"]`), nil
	}
	if err := d.Available(context.Background()); err == nil || !strings.Contains(err.Error(), "not rootless") {
		t.Fatalf("rootful daemon passed required-rootless policy: %v", err)
	}
	d.dockerCommand = func(_ context.Context, _ ...string) ([]byte, error) {
		return []byte(`["name=seccomp,profile=builtin","name=rootless"]`), nil
	}
	if err := d.Available(context.Background()); err != nil {
		t.Fatalf("rootless daemon rejected: %v", err)
	}
}

func TestAvailableRequiresIsolatedDaemonLabelWhenConfigured(t *testing.T) {
	d := NewLocalDocker()
	d.RequireIsolatedDaemon = true
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		switch args[len(args)-1] {
		case "{{json .SecurityOptions}}":
			return []byte(`["name=seccomp,profile=builtin","name=cgroupns"]`), nil
		case "{{json .Labels}}":
			return []byte(`["unrelated=value"]`), nil
		default:
			t.Fatalf("unexpected Docker probe: %v", args)
			return nil, nil
		}
	}
	if err := d.Available(context.Background()); err == nil || !strings.Contains(err.Error(), "not the isolated validator daemon") {
		t.Fatalf("unlabelled daemon passed required-isolated policy: %v", err)
	}
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		switch args[len(args)-1] {
		case "{{json .SecurityOptions}}":
			return []byte(`["name=seccomp,profile=builtin","name=cgroupns"]`), nil
		case "{{json .Labels}}":
			return []byte(`["io.heyditto.dittobench.isolated=true"]`), nil
		default:
			t.Fatalf("unexpected Docker probe: %v", args)
			return nil, nil
		}
	}
	if err := d.Available(context.Background()); err != nil {
		t.Fatalf("labelled isolated daemon rejected: %v", err)
	}
}

func TestV8IsolationReadyAcceptsAvailableCompatibilityExecutor(t *testing.T) {
	d := NewLocalDocker()
	d.HostGatewayIP = "192.0.2.10"
	d.RequireRootless = false
	d.dockerCommand = func(context.Context, ...string) ([]byte, error) {
		return []byte(`["name=seccomp,profile=builtin","name=cgroupns"]`), nil
	}
	if err := d.V8IsolationReady(context.Background()); err != nil {
		t.Fatalf("available compatibility executor rejected for v8: %v", err)
	}
}

func TestV8IsolationReadyEnforcesConfiguredRootlessPolicy(t *testing.T) {
	d := NewLocalDocker()
	d.HostGatewayIP = "192.0.2.10"
	d.RequireRootless = true
	d.dockerCommand = func(context.Context, ...string) ([]byte, error) {
		return []byte(`["name=seccomp,profile=builtin","name=rootless"]`), nil
	}
	if err := d.V8IsolationReady(context.Background()); err != nil {
		t.Fatalf("verified rootless executor rejected for v8: %v", err)
	}
}

func TestRunArgsRootlessUsesExplicitOuterGateway(t *testing.T) {
	d := NewLocalDocker()
	d.RequireRootless = true
	d.HostGatewayIP = "192.0.2.44"
	args := d.runArgsForNetwork("operator-image:latest", nil, "ditto-job-test", "abc123")
	if !hasFlagPair(args, "--add-host", "host.docker.internal:192.0.2.44") {
		t.Fatalf("rootless runtime did not bind the trusted outer gateway: %v", args)
	}
}

func TestCloneCredentialUsesEphemeralAskpassAndNeverHelperContents(t *testing.T) {
	tokenPath := filepath.Join(t.TempDir(), "github-token")
	const token = "secret-token-never-in-build-context"
	if err := os.WriteFile(tokenPath, []byte(token+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	d := NewLocalDocker()
	d.GitHubTokenFile = tokenPath
	env, cleanup, err := d.cloneEnvironment()
	if err != nil {
		t.Fatal(err)
	}
	askpass := ""
	for _, item := range env {
		if strings.HasPrefix(item, "GIT_ASKPASS=") {
			askpass = strings.TrimPrefix(item, "GIT_ASKPASS=")
		}
	}
	if askpass == "" {
		t.Fatal("missing ephemeral askpass helper")
	}
	helper, err := os.ReadFile(askpass)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(helper), token) {
		t.Fatal("credential was written into askpass helper")
	}
	cleanup()
	if _, err := os.Stat(askpass); !os.IsNotExist(err) {
		t.Fatalf("askpass helper survived cleanup: %v", err)
	}
}

// hasFlagPair reports whether args contains the adjacent pair [flag, value].
func hasFlagPair(args []string, flag, value string) bool {
	for i := 0; i+1 < len(args); i++ {
		if args[i] == flag && args[i+1] == value {
			return true
		}
	}
	return false
}

func TestRunArgs_DefaultsHardenAndBound(t *testing.T) {
	d := NewLocalDocker()
	args := d.runArgs("img:latest", nil)

	// Always-on isolation: a pids bound, no-new-privileges, loopback publish.
	if !hasFlagPair(args, "--pids-limit", "512") {
		t.Errorf("expected --pids-limit 512, got %v", args)
	}
	if !hasFlagPair(args, "--security-opt", "no-new-privileges") {
		t.Errorf("expected --security-opt no-new-privileges, got %v", args)
	}
	if !hasFlagPair(args, "--ipc", "none") {
		t.Errorf("expected private IPC namespace, got %v", args)
	}
	if !hasFlagPair(args, "--log-driver", "local") ||
		!hasFlagPair(args, "--log-opt", "max-size=8m") ||
		!hasFlagPair(args, "--log-opt", "max-file=1") ||
		!hasFlagPair(args, "--log-opt", "compress=false") {
		t.Errorf("expected bounded local container logs, got %v", args)
	}
	if !hasFlagPair(args, "--publish", "127.0.0.1:0:8080") {
		t.Errorf("expected loopback publish, got %v", args)
	}
	// Runtime isolation is default-on even outside the production Compose stack.
	if slices.Contains(args, "--network") {
		t.Errorf("default must not attach an egress network, got %v", args)
	}
	if !hasFlagPair(args, "--cap-drop", "ALL") {
		t.Errorf("default must drop all capabilities, got %v", args)
	}
	for flag, value := range map[string]string{
		"--user":   "65532:65532",
		"--tmpfs":  "/tmp:rw,noexec,nosuid,nodev,size=512m",
		"--memory": "3g",
		"--cpus":   "2",
		"--ulimit": "nofile=1024:1024",
	} {
		if !hasFlagPair(args, flag, value) {
			t.Errorf("expected %s %s, got %v", flag, value, args)
		}
	}
	if !slices.Contains(args, "--read-only") || !slices.Contains(args, "--init") {
		t.Errorf("expected read-only rootfs and init, got %v", args)
	}
	if slices.Contains(args, "--rm") {
		t.Errorf("--rm would erase OOM evidence before diagnostics: %v", args)
	}
	// Swap and pull policy are hosted-v2 opt-ins; the shared sandbox is unchanged.
	if slices.Contains(args, "--memory-swap") || slices.Contains(args, "--pull") {
		t.Errorf("shared sandbox must keep Docker's default swap and pull policy, got %v", args)
	}
	for _, a := range args {
		if strings.HasPrefix(a, "HTTPS_PROXY=") || strings.HasPrefix(a, "HTTP_PROXY=") {
			t.Errorf("default must not inject a proxy, got %v", args)
		}
	}
	// The image is last.
	if args[len(args)-1] != "img:latest" {
		t.Errorf("image must be the final arg, got %v", args)
	}
}

func TestDiagnosticsCapturesSanitizedResourceEvidence(t *testing.T) {
	d := NewLocalDocker()
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		switch args[0] {
		case "inspect":
			return []byte(`{"Running":true,"OOMKilled":false,"ExitCode":0}`), nil
		case "exec":
			return []byte("__memory_events__\nlow 0\nhigh 0\nmax 2\noom 1\noom_kill 1\n__memory_peak__\n3221225472\n__tmpfs__\ntmpfs 524288 524288 0 100% /tmp\n"), nil
		default:
			t.Fatalf("unexpected docker command: %v", args)
			return nil, nil
		}
	}

	diagnostics := d.Diagnostics(context.Background(), &Handle{ContainerID: "opaque"})
	if diagnostics.InfrastructureCode() != "sandbox_oom" {
		t.Fatalf("expected sandbox_oom, got %+v", diagnostics)
	}
	if diagnostics.MemoryPeakBytes == nil || *diagnostics.MemoryPeakBytes != 3221225472 {
		t.Fatalf("memory peak missing: %+v", diagnostics)
	}
	if diagnostics.TmpfsUsedBytes == nil || *diagnostics.TmpfsUsedBytes != 512<<20 {
		t.Fatalf("tmpfs usage missing: %+v", diagnostics)
	}
	if diagnostics.TmpfsCapacityBytes == nil || *diagnostics.TmpfsCapacityBytes != 512<<20 {
		t.Fatalf("tmpfs capacity missing: %+v", diagnostics)
	}
}

func TestDiagnosticsNonOOMExitIsNotInfrastructure(t *testing.T) {
	d := NewLocalDocker()
	d.dockerCommand = func(_ context.Context, _ ...string) ([]byte, error) {
		return []byte(`{"Running":false,"OOMKilled":false,"ExitCode":2}`), nil
	}
	diagnostics := d.Diagnostics(context.Background(), &Handle{ContainerID: "opaque"})
	if diagnostics.ExitCode != 2 || diagnostics.InfrastructureCode() != "" {
		t.Fatalf("ordinary exit must remain non-infrastructure: %+v", diagnostics)
	}
}

func TestRunArgs_HostedSwapAndPullPolicy(t *testing.T) {
	d := NewLocalDocker()
	d.MemoryLimit = "2147483648"
	d.MemorySwapLimit = "2147483648"
	d.PullNever = true
	args := d.runArgsForNetwork("operator-image:latest", nil, "ditto-job-test", "abc123")
	if !hasFlagPair(args, "--memory", "2147483648") || !hasFlagPair(args, "--memory-swap", "2147483648") {
		t.Errorf("expected swap limit equal to the memory limit, got %v", args)
	}
	if !hasFlagPair(args, "--pull", "never") {
		t.Errorf("expected --pull never, got %v", args)
	}
	if args[len(args)-1] != "operator-image:latest" {
		t.Errorf("image must be the final arg, got %v", args)
	}
}

func TestRunArgs_ZeroValuePidsStillBounded(t *testing.T) {
	// A directly-constructed LocalDocker (PidsLimit == 0) must not become
	// --pids-limit 0 (which docker treats as UNLIMITED).
	d := &LocalDocker{HarnessPort: "8080", MemoryLimit: "2g", CPULimit: "2"}
	if !hasFlagPair(d.runArgs("img", nil), "--pids-limit", "512") {
		t.Errorf("zero PidsLimit must default to 512, not 0/unlimited")
	}
}

func TestRunArgs_HardenDropsCaps(t *testing.T) {
	d := NewLocalDocker()
	d.Harden = true
	if !hasFlagPair(d.runArgs("img", nil), "--cap-drop", "ALL") {
		t.Errorf("Harden must add --cap-drop ALL")
	}
}

func TestRunArgs_CanaryHasNoDockerOrHostMount(t *testing.T) {
	d := NewLocalDocker()
	args := d.runArgs("security-canary:latest", map[string]string{"CANARY": "inert"})
	for _, forbidden := range []string{"--privileged", "--pid=host", "--ipc=host", "--network=host", "/var/run/docker.sock", "/:/"} {
		if slices.Contains(args, forbidden) || strings.Contains(strings.Join(args, " "), forbidden) {
			t.Fatalf("runtime arguments exposed forbidden host surface %q: %v", forbidden, args)
		}
	}
}

func TestRunArgs_ExplicitLSMProfiles(t *testing.T) {
	d := NewLocalDocker()
	d.SeccompProfile = "/etc/ditto/seccomp.json"
	d.AppArmorProfile = "ditto-untrusted"
	args := d.runArgs("img", nil)
	if !hasFlagPair(args, "--security-opt", "seccomp=/etc/ditto/seccomp.json") {
		t.Fatalf("seccomp profile missing: %v", args)
	}
	if !hasFlagPair(args, "--security-opt", "apparmor=ditto-untrusted") {
		t.Fatalf("AppArmor profile missing: %v", args)
	}
}

func TestRunArgs_EgressNetworkAttached(t *testing.T) {
	d := NewLocalDocker()
	d.EgressNetwork = "ditto-sandbox"
	if !hasFlagPair(d.runArgs("img", nil), "--network", "ditto-sandbox") {
		t.Errorf("EgressNetwork must attach --network")
	}
}

func TestRunArgs_EgressProxyInjectsEnv(t *testing.T) {
	d := NewLocalDocker()
	d.EgressProxy = "http://egress:3128"
	args := d.runArgs("img", map[string]string{"OPENROUTER_API_KEY": "sk-x"})

	if !hasFlagPair(args, "-e", "HTTPS_PROXY=http://egress:3128") {
		t.Errorf("EgressProxy must inject HTTPS_PROXY, got %v", args)
	}
	if !hasFlagPair(args, "-e", "HTTP_PROXY=http://egress:3128") {
		t.Errorf("EgressProxy must inject HTTP_PROXY, got %v", args)
	}
	// Loopback + host gateway must bypass the proxy.
	if !hasFlagPair(args, "-e", "NO_PROXY=host.docker.internal,localhost,127.0.0.1") {
		t.Errorf("EgressProxy must inject NO_PROXY, got %v", args)
	}
	// The caller's env still rides along.
	if !hasFlagPair(args, "-e", "OPENROUTER_API_KEY=sk-x") {
		t.Errorf("caller env must be preserved, got %v", args)
	}
}

func TestRunArgs_BrokerCapabilityHostUsesGatewayAndBypassesProxy(t *testing.T) {
	d := NewLocalDocker()
	d.RequireRootless = true
	d.HostGatewayIP = "192.0.2.44"
	d.EgressProxy = "http://egress:3128"
	host := "c-" + strings.Repeat("a", 52) + brokerCapabilityHostSuffix
	args := d.runArgs("img", map[string]string{
		"DITTOBENCH_INFERENCE_BASE_URL": "http://" + host + ":11436/v1/inference",
		"OLLAMA_BASE_URL":               "http://" + host + ":11436",
	})
	if !hasFlagPair(args, "--add-host", host+":192.0.2.44") {
		t.Fatalf("broker capability host did not resolve to the trusted gateway: %v", args)
	}
	if !hasFlagPair(args, "-e", "NO_PROXY=host.docker.internal,localhost,127.0.0.1,"+host) {
		t.Fatalf("broker capability host did not bypass the egress proxy: %v", args)
	}
}

func TestBrokerCapabilityHostFromEnvRejectsDrift(t *testing.T) {
	host := "c-" + strings.Repeat("a", 52) + brokerCapabilityHostSuffix
	for name, env := range map[string]map[string]string{
		"mismatched": {
			"DITTOBENCH_INFERENCE_BASE_URL": "http://" + host + ":11436/v1/inference",
			"OLLAMA_BASE_URL":               "http://c-" + strings.Repeat("b", 52) + brokerCapabilityHostSuffix + ":11436",
		},
		"invalid alphabet": {
			"DITTOBENCH_INFERENCE_BASE_URL": "http://c-" + strings.Repeat("0", 52) + brokerCapabilityHostSuffix + ":11436/v1/inference",
			"OLLAMA_BASE_URL":               "http://c-" + strings.Repeat("0", 52) + brokerCapabilityHostSuffix + ":11436",
		},
		"partial": {
			"DITTOBENCH_INFERENCE_BASE_URL": "http://" + host + ":11436/v1/inference",
			"OLLAMA_BASE_URL":               "http://host.docker.internal:11436",
		},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := brokerCapabilityHostFromEnv(env); err == nil {
				t.Fatal("invalid broker capability host was accepted")
			}
		})
	}
}

func TestRunArgs_OpenRouterShimUsesHostGatewayAndPublicCABundle(t *testing.T) {
	d := NewLocalDocker()
	d.RequireRootless = true
	d.HostGatewayIP = "192.0.2.44"
	d.OpenRouterShimCABundleHostPath = "/var/lib/dittobench-openrouter-shim/ca-bundle.pem"
	d.EgressProxy = "http://egress:3128"
	args := d.runArgs("img:latest", map[string]string{
		"SSL_CERT_FILE":      "/attacker/ca.pem",
		"REQUESTS_CA_BUNDLE": "/attacker/requests.pem",
	})

	if !hasFlagPair(args, "--add-host", "openrouter.ai:192.0.2.44") {
		t.Fatalf("hardcoded OpenRouter did not resolve to the trusted gateway: %v", args)
	}
	wantMount := "type=bind,src=/var/lib/dittobench-openrouter-shim/ca-bundle.pem," +
		"dst=" + OpenRouterShimCABundlePath + ",readonly"
	if !hasFlagPair(args, "--mount", wantMount) {
		t.Fatalf("shim CA bundle was not mounted read-only: %v", args)
	}
	for _, key := range []string{
		"SSL_CERT_FILE",
		"REQUESTS_CA_BUNDLE",
		"CURL_CA_BUNDLE",
		"NODE_EXTRA_CA_CERTS",
	} {
		if !hasFlagPair(args, "-e", key+"="+OpenRouterShimCABundlePath) {
			t.Fatalf("%s did not use the validator CA bundle: %v", key, args)
		}
	}
	if slices.Contains(args, "SSL_CERT_FILE=/attacker/ca.pem") ||
		slices.Contains(args, "REQUESTS_CA_BUNDLE=/attacker/requests.pem") {
		t.Fatalf("caller overrode the shim trust root: %v", args)
	}
	if !hasFlagPair(args, "-e", "NO_PROXY=host.docker.internal,localhost,127.0.0.1,openrouter.ai") {
		t.Fatalf("hardcoded OpenRouter did not bypass the public egress proxy: %v", args)
	}
}

func TestCleanupStaleRemovesOnlyOwnedExplicitResources(t *testing.T) {
	d := NewLocalDocker()
	var calls [][]string
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		calls = append(calls, append([]string(nil), args...))
		switch {
		case reflect.DeepEqual(args, []string{"ps", "-aq", "--filter", "label=io.heyditto.dittobench.run"}):
			return []byte("container-a\ncontainer-b\n"), nil
		case reflect.DeepEqual(args, []string{"rm", "-f", "container-a", "container-b"}):
			return nil, nil
		case reflect.DeepEqual(args, []string{"network", "ls", "-q", "--filter", "label=io.heyditto.dittobench.run"}):
			return []byte("network-a\nnetwork-b\n"), nil
		case reflect.DeepEqual(args, []string{"network", "rm", "network-a"}),
			reflect.DeepEqual(args, []string{"network", "rm", "network-b"}):
			return nil, nil
		default:
			t.Fatalf("unexpected docker command: %v", args)
			return nil, nil
		}
	}

	if err := d.CleanupStale(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(calls) != 5 {
		t.Fatalf("expected five explicit docker calls, got %v", calls)
	}
}

// --- container log capture (benchmark failure evidence) ---------------------

func TestLogsReturnsBoundedTailOfContainerOutput(t *testing.T) {
	d := NewLocalDocker()
	var got []string
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		got = args
		return []byte(strings.Repeat("a", 10_000) + "BOOT FAILED"), nil
	}

	out := d.Logs(context.Background(), &Handle{ContainerID: "c1"})
	if !strings.HasSuffix(out, "BOOT FAILED") {
		t.Fatalf("tail did not keep the most recent output: %q", out[max(0, len(out)-40):])
	}
	// "…" + ContainerLogTailBytes bytes; the marker is multi-byte, so bound on
	// the payload rather than on the whole string.
	if len(out) > ContainerLogTailBytes+len("…") {
		t.Fatalf("log tail is unbounded: %d bytes", len(out))
	}
	if got[0] != "logs" || !hasFlagPair(got, "--tail", containerLogLines) {
		t.Fatalf("logs were not pre-bounded at the daemon: %v", got)
	}
}

func TestLogsReturnsEmptyWhenTheContainerProducedNothing(t *testing.T) {
	d := NewLocalDocker()
	d.dockerCommand = func(_ context.Context, _ ...string) ([]byte, error) {
		return []byte("   \n\n "), nil
	}
	if out := d.Logs(context.Background(), &Handle{ContainerID: "c1"}); out != "" {
		t.Fatalf("whitespace-only output must read as no evidence, got %q", out)
	}
}

func TestLogsReturnsEmptyWhenTheRuntimeCannotBeQueried(t *testing.T) {
	d := NewLocalDocker()
	d.dockerCommand = func(_ context.Context, _ ...string) ([]byte, error) {
		return nil, errors.New("no such container")
	}
	if out := d.Logs(context.Background(), &Handle{ContainerID: "gone"}); out != "" {
		t.Fatalf("unqueryable runtime must yield no evidence, got %q", out)
	}
	if out := d.Logs(context.Background(), nil); out != "" {
		t.Fatalf("nil handle must yield no evidence, got %q", out)
	}
}

// A harness that dumps its own environment on a boot failure must not carry an
// injected credential back out through the failure envelope.
func TestLogsRedactInjectedCredentialsAndPresignedQueries(t *testing.T) {
	d := NewLocalDocker()
	d.dockerCommand = func(_ context.Context, _ ...string) ([]byte, error) {
		return []byte(
			"env: SOME_API_KEY=super-secret-value-1234 " +
				"fetch https://storage.example/artifact.tar?X-Goog-Signature=abcdef failed",
		), nil
	}
	handle := &Handle{
		ContainerID: "c1",
		injectedSecrets: credentialEnvValues(map[string]string{
			"SOME_API_KEY": "super-secret-value-1234",
		}),
	}

	out := d.Logs(context.Background(), handle)
	if strings.Contains(out, "super-secret-value-1234") {
		t.Fatalf("injected credential survived redaction: %q", out)
	}
	if strings.Contains(out, "X-Goog-Signature") {
		t.Fatalf("presigned query string survived redaction: %q", out)
	}
	// Redaction must not shred the diagnostic content around the secret.
	if !strings.Contains(out, "https://storage.example/artifact.tar?<redacted>") ||
		!strings.Contains(out, "failed") {
		t.Fatalf("redaction destroyed diagnostic context: %q", out)
	}
}

func TestLogsRedactBrokerCapabilityInsideHostname(t *testing.T) {
	const capability = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrst"
	if len(capability) != 52 {
		t.Fatal("test capability must retain the production wire length")
	}
	host := "c-" + capability + brokerCapabilityHostSuffix
	d := NewLocalDocker()
	d.dockerCommand = func(_ context.Context, _ ...string) ([]byte, error) {
		return []byte("OPENAI_API_KEY=" + capability + " gateway=http://" + host + ":11436/v1/inference"), nil
	}
	handle := &Handle{
		ContainerID: "c1",
		injectedSecrets: credentialEnvValues(map[string]string{
			"OPENAI_API_KEY": capability,
		}),
	}

	out := d.Logs(context.Background(), handle)
	if strings.Contains(out, capability) || strings.Contains(out, host) {
		t.Fatalf("broker capability survived redaction: %q", out)
	}
	if !strings.Contains(out, "<redacted>") {
		t.Fatalf("broker capability was removed without a redaction marker: %q", out)
	}
}

// The v7 lock injects the fixed non-secret placeholder "ticket" under three
// credential-shaped keys. Masking it would replace a common word throughout
// every harness log for no security benefit.
func TestCredentialEnvValuesIgnoresShortPlaceholdersAndNonCredentialKeys(t *testing.T) {
	got := credentialEnvValues(map[string]string{
		"CHUTES_API_KEY":      "ticket",
		"OPENAI_API_KEY":      "ticket",
		"CHUTES_BASE_URL":     "http://host.docker.internal:11436/v1/inference",
		"DITTOBENCH_MODEL":    "some/long-model-identifier",
		"DITTOBENCH_DB":       "/tmp/dittobench.db",
		"REAL_SECRET_TOKEN":   "0123456789abcdef",
		"ANOTHER_PASSWORD_XX": "0123456789abcdef-longer",
	})
	want := []string{"0123456789abcdef-longer", "0123456789abcdef"}
	if !slices.Equal(got, want) {
		t.Fatalf("credential values = %v, want %v (longest first)", got, want)
	}
}

func TestDefaultBridgeGatewayRequiresSinglePrivateIPv4DefaultBridge(t *testing.T) {
	const valid = `{"Name":"bridge","Driver":"bridge","EnableIPv4":true,"EnableIPv6":false,"IPAM":{"Driver":"default","Options":null,"Config":[{"Subnet":"172.17.0.0/16","IPRange":"","Gateway":"172.17.0.1"}]},"Internal":false,"Options":{"com.docker.network.bridge.default_bridge":"true","com.docker.network.bridge.name":"docker0"}}`
	d := NewLocalDocker()
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		if !reflect.DeepEqual(args, []string{"network", "inspect", "--format", "{{json .}}", "bridge"}) {
			t.Fatalf("unexpected Docker probe: %v", args)
		}
		return []byte(valid + "\n"), nil
	}
	gateway, err := d.DefaultBridgeGateway(context.Background())
	if err != nil || gateway.String() != "172.17.0.1" {
		t.Fatalf("docker 29 default bridge rejected: %v %v", gateway, err)
	}
	for name, body := range map[string]string{
		"user_network":   strings.Replace(valid, `"Name":"bridge"`, `"Name":"ditto-job-1"`, 1),
		"not_default":    strings.Replace(valid, `default_bridge":"true"`, `default_bridge":"false"`, 1),
		"macvlan":        strings.Replace(valid, `"Driver":"bridge"`, `"Driver":"macvlan"`, 1),
		"internal":       strings.Replace(valid, `"Internal":false`, `"Internal":true`, 1),
		"public":         strings.NewReplacer("172.17.0.0/16", "8.8.8.0/24", "172.17.0.1", "8.8.8.1").Replace(valid),
		"outside_subnet": strings.Replace(valid, `"Gateway":"172.17.0.1"`, `"Gateway":"172.18.0.1"`, 1),
		"network_addr":   strings.Replace(valid, `"Gateway":"172.17.0.1"`, `"Gateway":"172.17.0.0"`, 1),
		"unmasked":       strings.Replace(valid, `"Subnet":"172.17.0.0/16"`, `"Subnet":"172.17.0.1/16"`, 1),
		"no_gateway":     strings.Replace(valid, `"Gateway":"172.17.0.1"`, `"Gateway":""`, 1),
		"ipv6_gateway":   strings.NewReplacer("172.17.0.0/16", "fd00::/64", "172.17.0.1", "fd00::1").Replace(valid),
		"dual_stack":     strings.Replace(valid, `"Gateway":"172.17.0.1"}]`, `"Gateway":"172.17.0.1"},{"Subnet":"fd00::/64","Gateway":"fd00::1"}]`, 1),
		"two_documents":  valid + valid,
		"empty":          "",
	} {
		t.Run(name, func(t *testing.T) {
			d.dockerCommand = func(context.Context, ...string) ([]byte, error) { return []byte(body), nil }
			if gateway, err := d.DefaultBridgeGateway(context.Background()); err == nil {
				t.Fatalf("invalid default bridge accepted as %s", gateway)
			}
		})
	}
	d.dockerCommand = func(context.Context, ...string) ([]byte, error) { return nil, errors.New("daemon down") }
	if _, err := d.DefaultBridgeGateway(context.Background()); err == nil {
		t.Fatal("unavailable daemon produced a gateway")
	}
}

func TestDefaultBridgeGatewayFromSocketUsesOneReadOnlyEngineRequest(t *testing.T) {
	const valid = `{"Name":"bridge","Id":"0123","Driver":"bridge","EnableIPv4":true,"IPAM":{"Driver":"default","Config":[{"Subnet":"172.17.0.0/16","Gateway":"172.17.0.1"}]},"Internal":false,"Options":{"com.docker.network.bridge.default_bridge":"true"}}`
	root, err := os.MkdirTemp("", "bridge-api-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	socket := filepath.Join(root, "docker.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	status, body := http.StatusOK, valid
	var requests []string
	var mu sync.Mutex
	server := &http.Server{Handler: http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		mu.Lock()
		requests = append(requests, request.Method+" "+request.URL.String())
		code, reply := status, body
		mu.Unlock()
		response.WriteHeader(code)
		_, _ = response.Write([]byte(reply))
	})}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })
	set := func(code int, reply string) {
		mu.Lock()
		status, body = code, reply
		mu.Unlock()
	}

	gateway, err := DefaultBridgeGatewayFromSocket(t.Context(), socket)
	if err != nil || gateway != netip.MustParseAddr("172.17.0.1") {
		t.Fatalf("engine default bridge rejected: %v %v", gateway, err)
	}
	mu.Lock()
	if !slices.Equal(requests, []string{"GET /networks/bridge"}) {
		t.Fatalf("unexpected engine requests: %v", requests)
	}
	mu.Unlock()
	for name, reply := range map[string]struct {
		code int
		body string
	}{
		"not_found":   {http.StatusNotFound, valid},
		"redirect":    {http.StatusFound, valid},
		"user_bridge": {http.StatusOK, strings.Replace(valid, `"Name":"bridge"`, `"Name":"ditto-job-1"`, 1)},
		"not_default": {http.StatusOK, strings.Replace(valid, `default_bridge":"true"`, `default_bridge":"false"`, 1)},
		"oversized":   {http.StatusOK, valid + strings.Repeat(" ", 1<<20)},
	} {
		t.Run(name, func(t *testing.T) {
			set(reply.code, reply.body)
			if gateway, err := DefaultBridgeGatewayFromSocket(t.Context(), socket); err == nil {
				t.Fatalf("invalid engine reply accepted as %s", gateway)
			}
		})
	}
	set(http.StatusOK, valid)
	for _, bad := range []string{"docker.sock", socket + "/", filepath.Join(root, "missing.sock")} {
		if _, err := DefaultBridgeGatewayFromSocket(t.Context(), bad); err == nil {
			t.Fatalf("socket %q accepted", bad)
		}
	}
}

// Hosted rootless-netns routing sets HostGatewayIP to the RootlessKit bridge
// gateway. Candidates must resolve every broker name to it, not to eth0.
func TestRunArgsRootlessNamespaceGatewayMapsHostDockerInternal(t *testing.T) {
	d := NewLocalDocker()
	d.RequireRootless = true
	d.HostGatewayIP = "172.17.0.1"
	d.EgressProxy = "http://10.33.0.2:18090"
	args := d.runArgsForNetwork("operator-image:latest", nil, "ditto-job-test", "abc123")
	if !hasFlagPair(args, "--add-host", "host.docker.internal:172.17.0.1") || hasFlagPair(args, "--add-host", "host.docker.internal:host-gateway") {
		t.Fatalf("rootless namespace gateway not bound: %v", args)
	}
	if !hasFlagPair(args, "--network", "ditto-job-test") || !hasFlagPair(args, "-e", "NO_PROXY=host.docker.internal,localhost,127.0.0.1") {
		t.Fatalf("router traffic is not direct on the per-run bridge: %v", args)
	}
}
