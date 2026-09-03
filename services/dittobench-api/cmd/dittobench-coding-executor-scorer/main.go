// Binary dittobench-coding-executor-scorer is the immutable, dedicated
// artifact for a future coding executor host. It exposes a constant liveness
// endpoint and validator-signed control requests on one private Unix socket,
// and refuses to start unless an operator enables its deployment profile. It
// owns no wallet, provider or Platform credential, assignment, or TCP listener.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontrol"
	"github.com/ditto-assistant/dittobench-api/internal/codinghost"
	"github.com/ditto-assistant/dittobench-api/internal/release"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

const (
	defaultSocketPath             = "/run/ditto-coding-scorer/control.sock"
	enableEnvironment             = "DITTOBENCH_CODING_EXECUTOR_SCORER_ENABLED"
	runtimeImageDigestEnvironment = "DITTOBENCH_CODING_RUNTIME_IMAGE_DIGEST"
	validatorHotkeyEnvironment    = "DITTOBENCH_CODING_EXECUTOR_VALIDATOR_HOTKEY"
	sourceGatewayEnvironment      = "DITTOBENCH_SANDBOX_HOST_GATEWAY_IP"
	sourceHostname                = "host.docker.internal"
	sourcePort                    = 11438
	policyFile                    = "/opt/ditto/coding/coding_inference_policy_locked_v1.json"
	policySHA256                  = "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
)

var sha256ImageDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

type configuration struct {
	socketPath      string
	sourceGateway   string
	validatorHotkey string
	enabled         bool
}

func configurationFromEnvironment(socketPath string, getenv func(string) string) (configuration, error) {
	if socketPath == "" || !filepath.IsAbs(socketPath) || filepath.Clean(socketPath) != defaultSocketPath {
		return configuration{}, errors.New("coding executor scorer socket path is invalid")
	}
	if strings.EqualFold(strings.TrimSpace(getenv(enableEnvironment)), "true") {
		gateway, err := sourceGatewayFromEnvironment(getenv)
		if err != nil {
			return configuration{}, err
		}
		hotkey := strings.TrimSpace(getenv(validatorHotkeyEnvironment))
		if !codingcontrol.ValidValidatorHotkey(hotkey) {
			return configuration{}, errors.New("coding executor scorer validator authority is invalid")
		}
		return configuration{
			socketPath: socketPath, sourceGateway: gateway,
			validatorHotkey: hotkey, enabled: true,
		}, nil
	}
	return configuration{}, errors.New("coding executor scorer is disabled")
}

func sourceGatewayFromEnvironment(getenv func(string) string) (string, error) {
	value := strings.TrimSpace(getenv(sourceGatewayEnvironment))
	parsed := net.ParseIP(value)
	if parsed == nil || parsed.To4() == nil || parsed.IsLoopback() || parsed.IsUnspecified() ||
		parsed.IsMulticast() || parsed.IsLinkLocalUnicast() {
		return "", errors.New("coding executor scorer source gateway is invalid")
	}
	return parsed.String(), nil
}

func sourceListenerAddress(gateway string) string {
	return net.JoinHostPort(gateway, strconv.Itoa(sourcePort))
}

func sourcePublicBaseURL() string {
	return "http://" + sourceHostname + ":" + strconv.Itoa(sourcePort)
}

func controlMux(ingress *codingcontrol.Ingress) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(response http.ResponseWriter, _ *http.Request) {
		response.Header().Set("Cache-Control", "no-store")
		response.WriteHeader(http.StatusNoContent)
	})
	if ingress != nil {
		mux.Handle("/v1/coding/", ingress.Handler())
	}
	return mux
}

func privateControlRoutes(host *codinghost.Host) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("POST /v1/coding/supervisor/{operation}", host.SupervisorHandler())
	mux.Handle("POST /v1/coding/publications/{operation}", host.PublicationHandler())
	return mux
}

func loadPolicy(path string) (codingcontract.InferencePolicy, error) {
	var zero codingcontract.InferencePolicy
	if !filepath.IsAbs(path) || filepath.Clean(path) == string(filepath.Separator) {
		return zero, errors.New("coding executor scorer policy path is invalid")
	}
	handle, err := os.Open(path)
	if err != nil {
		return zero, errors.New("coding executor scorer policy is unavailable")
	}
	defer handle.Close()
	body, err := io.ReadAll(io.LimitReader(handle, codingcontract.MaxInferencePolicyBytes+1))
	if err != nil || len(body) == 0 || len(body) > codingcontract.MaxInferencePolicyBytes {
		return zero, errors.New("coding executor scorer policy is invalid")
	}
	policy, err := codingcontract.ParseInferencePolicy(body)
	if err != nil {
		return zero, errors.New("coding executor scorer policy is invalid")
	}
	digest, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil || digest != policySHA256 {
		return zero, errors.New("coding executor scorer policy is not locked")
	}
	return policy, nil
}

func buildHost(config configuration) (*codinghost.Host, net.Listener, string, error) {
	tokenPath := strings.TrimSpace(os.Getenv("DITTOBENCH_CODING_EXECUTOR_CONTROL_TOKEN_FILE"))
	privateRoot := strings.TrimSpace(os.Getenv("DITTOBENCH_CODING_EXECUTOR_PRIVATE_ROOT"))
	imageRepository := strings.TrimSpace(os.Getenv("DITTOBENCH_CODING_RUNTIME_IMAGE_REPOSITORY"))
	imageDigest := strings.TrimSpace(os.Getenv(runtimeImageDigestEnvironment))
	if !filepath.IsAbs(tokenPath) || !filepath.IsAbs(privateRoot) || imageRepository == "" || !sha256ImageDigest.MatchString(imageDigest) ||
		!strings.EqualFold(strings.TrimSpace(os.Getenv("DITTOBENCH_REQUIRE_ROOTLESS_DOCKER")), "true") ||
		!strings.EqualFold(strings.TrimSpace(os.Getenv("DITTOBENCH_REQUIRE_ISOLATED_DOCKER_DAEMON")), "true") ||
		strings.TrimSpace(os.Getenv("DOCKER_HOST")) != "unix:///run/ditto-coding-executor/docker.sock" {
		return nil, nil, "", errors.New("coding executor scorer runtime profile is incomplete")
	}
	token, err := os.ReadFile(tokenPath)
	if err != nil || len(strings.TrimSpace(string(token))) < 32 {
		return nil, nil, "", errors.New("coding executor scorer token is unavailable")
	}
	policy, err := loadPolicy(policyFile)
	if err != nil {
		return nil, nil, "", err
	}
	source, err := net.Listen("tcp4", sourceListenerAddress(config.sourceGateway))
	if err != nil {
		return nil, nil, "", errors.New("coding executor scorer source listener is unavailable")
	}
	docker := sandbox.NewLocalDocker()
	docker.HostGatewayIP = config.sourceGateway
	host, err := codinghost.New(codinghost.Config{
		ControlToken: strings.TrimSpace(string(token)), PrivateRoot: privateRoot,
		SourceListener: source, SourcePublicBaseURL: sourcePublicBaseURL(), Policy: policy,
		RuntimeImageRepository: imageRepository, RuntimeImageDigest: imageDigest, Docker: docker,
		CandidateUID: 65532, CandidateGID: 65532, MaxTotalBytes: 16 << 30,
		JournalMaxTotalBytes: 3 << 30, MaxAttempts: 64, Now: time.Now,
	})
	if err != nil {
		_ = source.Close()
		return nil, nil, "", errors.New("coding executor scorer host is unavailable")
	}
	return host, source, strings.TrimSpace(string(token)), nil
}

func listenUnix(path string) (net.Listener, error) {
	info, err := os.Lstat(path)
	if err == nil {
		metadata, ok := info.Sys().(*syscall.Stat_t)
		if !ok || info.Mode()&os.ModeSocket == 0 || info.Mode()&os.ModeSymlink != 0 || metadata.Uid != uint32(os.Geteuid()) {
			return nil, errors.New("coding executor scorer socket cannot be replaced")
		}
		if err := os.Remove(path); err != nil {
			return nil, errors.New("coding executor scorer stale socket cannot be removed")
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, errors.New("coding executor scorer socket is unavailable")
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, errors.New("coding executor scorer socket cannot listen")
	}
	if err := os.Chmod(path, 0o600); err != nil {
		_ = listener.Close()
		return nil, errors.New("coding executor scorer socket mode cannot be set")
	}
	return listener, nil
}

func main() {
	socketPath := flag.String("socket", defaultSocketPath, "fixed Unix control socket")
	version := flag.Bool("version", false, "print immutable artifact provenance")
	flag.Parse()
	if *version {
		if err := json.NewEncoder(os.Stdout).Encode(release.Resolve(os.Getenv)); err != nil {
			fmt.Fprintln(os.Stderr, "coding executor scorer cannot report version")
			os.Exit(111)
		}
		return
	}
	config, err := configurationFromEnvironment(*socketPath, os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "coding executor scorer is not enabled")
		os.Exit(78)
	}
	host, source, controlToken, err := buildHost(config)
	if err != nil {
		fmt.Fprintln(os.Stderr, "coding executor scorer runtime is unavailable")
		os.Exit(78)
	}
	defer source.Close()
	defer func() { _ = host.Close(context.Background()) }()
	ingress, err := codingcontrol.New(codingcontrol.Config{
		Downstream: privateControlRoutes(host), ControlToken: controlToken,
		ValidatorHotkey: config.validatorHotkey, Now: time.Now,
		VerifySignature: codingcontract.VerifyExecutorSR25519, MaximumNonces: 65536,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "coding executor scorer control ingress is unavailable")
		os.Exit(78)
	}
	listener, err := listenUnix(config.socketPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "coding executor scorer socket is unavailable")
		os.Exit(111)
	}
	defer listener.Close()
	if err := http.Serve(listener, controlMux(ingress)); err != nil && !errors.Is(err, http.ErrServerClosed) {
		fmt.Fprintln(os.Stderr, "coding executor scorer stopped")
		os.Exit(111)
	}
}
