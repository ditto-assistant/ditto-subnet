package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinghost"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

const (
	defaultCodingPolicyFile  = "/opt/ditto/coding/coding_inference_policy_locked_v1.json"
	lockedCodingPolicySHA256 = "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
	// codingDockerHostEnvironment selects the dedicated rootless coding daemon.
	// Coding harnesses and executors never use the stack's DOCKER_HOST, which on
	// the Compose validator is the privileged rootful sandbox-docker daemon that
	// ordinary scoring uses.
	codingDockerHostEnvironment = "DITTOBENCH_CODING_DOCKER_HOST"
	// The coding runtime's own container settings. They never fall back to the
	// ordinary scorer's DITTOBENCH_SANDBOX_* values, and the coding runtime has
	// no CA bundle setting at all.
	codingEgressNetworkEnvironment = "DITTOBENCH_CODING_EGRESS_NETWORK"
	codingEgressProxyEnvironment   = "DITTOBENCH_CODING_EGRESS_PROXY"
	codingHostGatewayEnvironment   = "DITTOBENCH_CODING_HOST_GATEWAY_IP"
	codingSeccompEnvironment       = "DITTOBENCH_CODING_SECCOMP_PROFILE"
	codingAppArmorEnvironment      = "DITTOBENCH_CODING_APPARMOR_PROFILE"
)

// codingRuntimeDockerFromEnvironment builds the coding runtime's Docker client
// from DITTOBENCH_CODING_* settings only. A coding proxy or gateway equal to the
// ordinary scorer's configured value is refused, so the two runtimes cannot be
// silently pointed at one shared egress path.
func codingRuntimeDockerFromEnvironment(getenv func(string) string, dockerHost string) (*sandbox.LocalDocker, error) {
	settings := sandbox.CodingRuntimeSettings{
		DockerHost:      dockerHost,
		EgressNetwork:   strings.TrimSpace(getenv(codingEgressNetworkEnvironment)),
		EgressProxy:     strings.TrimSpace(getenv(codingEgressProxyEnvironment)),
		HostGatewayIP:   strings.TrimSpace(getenv(codingHostGatewayEnvironment)),
		SeccompProfile:  strings.TrimSpace(getenv(codingSeccompEnvironment)),
		AppArmorProfile: strings.TrimSpace(getenv(codingAppArmorEnvironment)),
	}
	sharedProxy := strings.TrimSpace(getenv("DITTOBENCH_SANDBOX_EGRESS_PROXY"))
	sharedGateway := strings.TrimSpace(getenv("DITTOBENCH_SANDBOX_HOST_GATEWAY_IP"))
	docker, err := sandbox.NewCodingRuntimeDocker(settings)
	if err != nil || (sharedProxy != "" && sharedProxy == settings.EgressProxy) ||
		(sharedGateway != "" && sharedGateway == settings.HostGatewayIP) {
		return nil, errors.New("coding runtime requires its own " + codingEgressNetworkEnvironment + ", " +
			codingEgressProxyEnvironment + " (http://<private IPv4>:<port>) and " + codingHostGatewayEnvironment +
			" (private IPv4), distinct from the sandbox settings")
	}
	return docker, nil
}

// installCodingHost isolates the default-off coding runtime from ordinary
// scoring. A construction failure disables every coding route (they answer
// 404, so the validator shadow worker refuses) and is logged, but the scorer
// keeps serving benchmark tickets.
//
// The scorer serves no certification canary route. Certification runs only in
// the host certification service (internal/codingcertservice), which the
// validator reaches over its fixed Unix socket; the retired Compose route
// could never pass readiness v2's topology proof.
func installCodingHost(
	build func() (*codinghost.Host, error),
	logf func(string, ...any),
) *codinghost.Host {
	host, err := build()
	if err != nil {
		logf("coding runtime refused; coding routes stay disabled and ordinary scoring continues: %v", err)
		return nil
	}
	if host != nil {
		logf("shadow coding runtime enabled on private control and source-bound routes")
	}
	return host
}

// codingDockerHostFromEnvironment returns the dedicated coding daemon endpoint.
// It refuses an unset value, anything but a local Unix socket, the
// conventional rootful sockets, and the ordinary scoring daemon itself.
func codingDockerHostFromEnvironment(getenv func(string) string) (string, error) {
	endpoint := strings.TrimSpace(getenv(codingDockerHostEnvironment))
	if !codingexecutor.ValidDedicatedDockerHost(endpoint) ||
		endpoint == strings.TrimSpace(getenv("DOCKER_HOST")) {
		return "", errors.New("coding runtime requires DITTOBENCH_CODING_DOCKER_HOST to name a dedicated rootless daemon socket, not the sandbox daemon")
	}
	return endpoint, nil
}

func codingShadowHostFromEnvironment(apiPort int, brokerPort int) (*codinghost.Host, error) {
	if !envBool("DITTOBENCH_CODING_SHADOW_ENABLED") {
		return nil, nil
	}
	dockerHost, err := codingDockerHostFromEnvironment(os.Getenv)
	if err != nil {
		return nil, err
	}
	// Before any side effect: the coding runtime's own proxy, gateway and
	// network, with no CA bundle and no fallback to the sandbox settings.
	docker, err := codingRuntimeDockerFromEnvironment(os.Getenv, dockerHost)
	if err != nil {
		return nil, err
	}
	port, err := strictCodingEnvInt("DITTOBENCH_CODING_SOURCE_PORT", 11438)
	if err != nil {
		return nil, err
	}
	if port < 1024 || port > 65535 || port == apiPort || port == brokerPort {
		return nil, errors.New("coding shadow source port is invalid")
	}
	policy, err := loadCodingInferencePolicy(envStringDefault(
		"DITTOBENCH_CODING_POLICY_FILE", defaultCodingPolicyFile,
	))
	if err != nil {
		return nil, err
	}
	root := filepath.Clean(envStringDefault(
		"DITTOBENCH_CODING_PRIVATE_ROOT",
		"/var/lib/dittobench-private-artifacts/coding-shadow-v1",
	))
	if !filepath.IsAbs(root) || root == string(filepath.Separator) {
		return nil, errors.New("coding shadow private root is invalid")
	}
	if err := os.Mkdir(root, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return nil, errors.New("coding shadow private root is unavailable")
	}
	listener, err := net.Listen("tcp4", "0.0.0.0:"+strconv.Itoa(port))
	if err != nil {
		return nil, errors.New("coding shadow source listener is unavailable")
	}
	publicBase := envStringDefault(
		"DITTOBENCH_CODING_SOURCE_PUBLIC_BASE_URL",
		"http://host.docker.internal:"+strconv.Itoa(port),
	)
	candidateUID, uidErr := strictCodingEnvInt("DITTOBENCH_CODING_CANDIDATE_UID", 65532)
	candidateGID, gidErr := strictCodingEnvInt("DITTOBENCH_CODING_CANDIDATE_GID", 65532)
	if uidErr != nil || gidErr != nil {
		_ = listener.Close()
		return nil, errors.New("coding shadow candidate identity is invalid")
	}
	if candidateUID <= 0 || candidateGID <= 0 || candidateUID > 1<<31 || candidateGID > 1<<31 {
		_ = listener.Close()
		return nil, errors.New("coding shadow candidate identity is invalid")
	}
	// A dedicated runtime: the ordinary scorer's LocalDocker stays on the stack
	// daemon, while every coding harness and executor call reaches only the
	// rootless daemon, which must also prove rootless isolation live.
	host, err := codinghost.New(codinghost.Config{
		ControlToken: controlTokenFromEnv(), PrivateRoot: root,
		SourceListener: listener, SourcePublicBaseURL: publicBase, Policy: policy,
		RuntimeImageRepository: strings.TrimSpace(os.Getenv("DITTOBENCH_CODING_RUNTIME_IMAGE_REPOSITORY")),
		RuntimeImageDigest:     strings.TrimSpace(os.Getenv("DITTOBENCH_CODING_RUNTIME_IMAGE_DIGEST")),
		Docker:                 docker, CandidateUID: uint32(candidateUID), CandidateGID: uint32(candidateGID),
		MaxTotalBytes: 16 << 30, JournalMaxTotalBytes: 3 << 30, MaxAttempts: 64,
	})
	if err != nil {
		_ = listener.Close()
		return nil, err
	}
	return host, nil
}

func loadCodingInferencePolicy(path string) (codingcontract.InferencePolicy, error) {
	var zero codingcontract.InferencePolicy
	if !filepath.IsAbs(path) || filepath.Clean(path) == string(filepath.Separator) {
		return zero, errors.New("coding inference policy path is invalid")
	}
	handle, err := os.Open(path)
	if err != nil {
		return zero, errors.New("coding inference policy is unavailable")
	}
	defer handle.Close()
	body, err := io.ReadAll(io.LimitReader(handle, codingcontract.MaxInferencePolicyBytes+1))
	if err != nil || len(body) == 0 || len(body) > codingcontract.MaxInferencePolicyBytes {
		return zero, errors.New("coding inference policy envelope is invalid")
	}
	if err := codingcontract.ValidateJSONDocument(body, codingcontract.MaxInferencePolicyBytes); err != nil {
		return zero, errors.New("coding inference policy envelope is invalid")
	}
	policy, err := codingcontract.ParseInferencePolicy(body)
	if err != nil {
		return zero, errors.New("coding inference policy is invalid")
	}
	digest, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil || digest != lockedCodingPolicySHA256 {
		return zero, errors.New("coding inference policy digest is not locked")
	}
	return policy, nil
}

func envStringDefault(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func strictCodingEnvInt(name string, fallback int) (int, error) {
	raw, present := os.LookupEnv(name)
	if !present || strings.TrimSpace(raw) == "" {
		return fallback, nil
	}
	value, err := strconv.Atoi(strings.TrimSpace(raw))
	if err != nil || value <= 0 {
		return 0, errors.New("coding shadow integer configuration is invalid")
	}
	return value, nil
}

func closeCodingShadowHost(host *codinghost.Host) {
	if host == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := host.Close(ctx); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, "coding shadow host cleanup failed")
	}
}

func codingHandler(host *codinghost.Host, publication bool) http.Handler {
	if host == nil {
		return http.NotFoundHandler()
	}
	if publication {
		return host.PublicationHandler()
	}
	return host.SupervisorHandler()
}
