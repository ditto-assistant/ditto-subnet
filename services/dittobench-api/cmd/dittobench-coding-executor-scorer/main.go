// Binary dittobench-coding-executor-scorer is the immutable, dedicated
// artifact for a future coding executor host. It exposes a constant liveness
// endpoint, ticketless readiness, and validator-signed control requests on one
// private Unix socket.
// A separate default-off mode terminates the dedicated mTLS transport and
// forwards only to that socket. Another one-shot mode verifies one ticket-bound
// S3 artifact without constructing the scorer host. It owns no wallet, provider
// or Platform credential, assignment, or public listener.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
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

	"github.com/ditto-assistant/dittobench-api/internal/codingartifactcanary"
	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontrol"
	"github.com/ditto-assistant/dittobench-api/internal/codinghost"
	"github.com/ditto-assistant/dittobench-api/internal/codingtransport"
	"github.com/ditto-assistant/dittobench-api/internal/release"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

const (
	defaultSocketPath             = "/run/ditto-coding-scorer/control.sock"
	enableEnvironment             = "DITTOBENCH_CODING_EXECUTOR_SCORER_ENABLED"
	runtimeImageDigestEnvironment = "DITTOBENCH_CODING_RUNTIME_IMAGE_DIGEST"
	validatorHotkeyEnvironment    = "DITTOBENCH_CODING_EXECUTOR_VALIDATOR_HOTKEY"
	mtlsEnableEnvironment         = "DITTOBENCH_CODING_EXECUTOR_MTLS_TRANSPORT_ENABLED"
	mtlsBindEnvironment           = "DITTOBENCH_CODING_EXECUTOR_MTLS_BIND_ADDRESS"
	mtlsSourceCIDREnvironment     = "DITTOBENCH_CODING_EXECUTOR_MTLS_SOURCE_CIDR"
	artifactCanaryEnvironment     = "DITTOBENCH_CODING_EXECUTOR_ARTIFACT_CANARY_ENABLED"
	sourceGatewayEnvironment      = "DITTOBENCH_SANDBOX_HOST_GATEWAY_IP"
	sourceHostname                = "host.docker.internal"
	sourcePort                    = 11438
	mtlsPort                      = 9443
	policyFile                    = "/opt/ditto/coding/coding_inference_policy_locked_v1.json"
	policySHA256                  = "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
	mtlsCACredential              = "validator-ca.pem"
	mtlsCertificateCredential     = "server-cert.pem"
	mtlsKeyCredential             = "server-key.pem"
	artifactCapabilityCredential  = "artifact-capability.json"
)

var sha256ImageDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

const (
	artifactCanaryRoot      = "/var/lib/ditto-coding-executor/artifact-canary"
	artifactCanaryTemporary = artifactCanaryRoot + "/tmp"
	artifactCanaryReceipt   = artifactCanaryRoot + "/receipt.json"
)

type configuration struct {
	socketPath      string
	sourceGateway   string
	validatorHotkey string
	enabled         bool
}

type mtlsConfiguration struct {
	bindAddress     string
	sourceCIDR      string
	validatorHotkey string
	credentials     string
}

type artifactCanaryConfiguration struct {
	capabilityPath string
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

func mtlsConfigurationFromEnvironment(getenv func(string) string) (mtlsConfiguration, error) {
	if !strings.EqualFold(strings.TrimSpace(getenv(mtlsEnableEnvironment)), "true") {
		return mtlsConfiguration{}, errors.New("coding executor mTLS transport is disabled")
	}
	bind := net.ParseIP(strings.TrimSpace(getenv(mtlsBindEnvironment)))
	sourceIP, sourceNetwork, sourceErr := net.ParseCIDR(
		strings.TrimSpace(getenv(mtlsSourceCIDREnvironment)),
	)
	hotkey := strings.TrimSpace(getenv(validatorHotkeyEnvironment))
	credentials := filepath.Clean(strings.TrimSpace(getenv("CREDENTIALS_DIRECTORY")))
	prefix, bits := 0, 0
	if sourceNetwork != nil {
		prefix, bits = sourceNetwork.Mask.Size()
	}
	if bind == nil || bind.To4() == nil || !bind.IsPrivate() || bind.IsLoopback() ||
		sourceErr != nil || sourceIP.To4() == nil || !sourceIP.IsPrivate() || prefix != 32 || bits != 32 ||
		!codingcontrol.ValidValidatorHotkey(hotkey) || !filepath.IsAbs(credentials) ||
		!strings.HasPrefix(credentials, "/run/credentials/") {
		return mtlsConfiguration{}, errors.New("coding executor mTLS transport profile is invalid")
	}
	return mtlsConfiguration{
		bindAddress: bind.String(), sourceCIDR: sourceNetwork.String(),
		validatorHotkey: hotkey, credentials: credentials,
	}, nil
}

func artifactCanaryConfigurationFromEnvironment(getenv func(string) string) (artifactCanaryConfiguration, error) {
	if !strings.EqualFold(strings.TrimSpace(getenv(artifactCanaryEnvironment)), "true") {
		return artifactCanaryConfiguration{}, errors.New("coding artifact canary is disabled")
	}
	credentials := filepath.Clean(strings.TrimSpace(getenv("CREDENTIALS_DIRECTORY")))
	if !filepath.IsAbs(credentials) || !strings.HasPrefix(credentials, "/run/credentials/") {
		return artifactCanaryConfiguration{}, errors.New("coding artifact canary profile is invalid")
	}
	return artifactCanaryConfiguration{
		capabilityPath: filepath.Join(credentials, artifactCapabilityCredential),
	}, nil
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
	mux.HandleFunc("GET "+codingtransport.ReadinessPath, func(response http.ResponseWriter, _ *http.Request) {
		response.Header().Set("Cache-Control", "no-store")
		response.Header().Set("Content-Type", "application/json")
		response.Header().Set("X-Content-Type-Options", "nosniff")
		if ingress == nil {
			response.WriteHeader(http.StatusServiceUnavailable)
			_, _ = response.Write([]byte(`{"error":"executor_not_ready"}` + "\n"))
			return
		}
		body := []byte(`{"schema":"dittobench-coding-executor-readiness-v1","coding_contract_version":1,"weight_eligible":false,"transport":"mtls","supervisor_ready":true,"publication_ready":true,"ticket_authority_used":false}` + "\n")
		response.Header().Set("Content-Length", strconv.Itoa(len(body)))
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(body)
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

func runMTLSProxy(config mtlsConfiguration) error {
	proxy, err := codingtransport.New(codingtransport.Config{
		ValidatorHotkey: config.validatorHotkey,
		UnixSocketPath:  codingtransport.ControlSocketPath,
	})
	if err != nil {
		return errors.New("coding executor mTLS proxy is unavailable")
	}
	certificate, err := tls.LoadX509KeyPair(
		filepath.Join(config.credentials, mtlsCertificateCredential),
		filepath.Join(config.credentials, mtlsKeyCredential),
	)
	if err != nil {
		return errors.New("coding executor mTLS server identity is unavailable")
	}
	ca, err := os.ReadFile(filepath.Join(config.credentials, mtlsCACredential))
	if err != nil || len(ca) == 0 || len(ca) > 1<<20 {
		return errors.New("coding executor mTLS validator CA is unavailable")
	}
	clientCAs := x509.NewCertPool()
	if !clientCAs.AppendCertsFromPEM(ca) {
		return errors.New("coding executor mTLS validator CA is invalid")
	}
	tlsConfig, err := codingtransport.ServerTLSConfig(certificate, clientCAs)
	if err != nil {
		return errors.New("coding executor mTLS configuration is invalid")
	}
	listener, err := net.Listen("tcp4", net.JoinHostPort(config.bindAddress, strconv.Itoa(mtlsPort)))
	if err != nil {
		return errors.New("coding executor mTLS listener is unavailable")
	}
	defer listener.Close()
	server := &http.Server{
		Handler: proxy.Handler(), ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout: 33 * time.Minute, WriteTimeout: 33 * time.Minute,
		IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10,
	}
	return server.Serve(tls.NewListener(listener, tlsConfig))
}

func runArtifactCanary(config artifactCanaryConfiguration) error {
	fetcher, err := codingartifacts.New(codingartifacts.Config{
		RequestTimeout:     2 * time.Minute,
		TemporaryDirectory: artifactCanaryTemporary,
		AllowLoopback:      false,
		Now:                time.Now,
	})
	if err != nil {
		return errors.New("coding artifact canary fetcher is unavailable")
	}
	canary, err := codingartifactcanary.New(codingartifactcanary.Config{
		Artifacts:      fetcher,
		CapabilityPath: config.capabilityPath,
		ReceiptPath:    artifactCanaryReceipt,
		Now:            time.Now,
	})
	if err != nil {
		return errors.New("coding artifact canary is unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	if err := canary.Run(ctx); err != nil {
		return errors.New("coding artifact canary fetch failed")
	}
	return nil
}

func main() {
	socketPath := flag.String("socket", defaultSocketPath, "fixed Unix control socket")
	mtlsProxy := flag.Bool("mtls-proxy", false, "run the default-off mTLS transport proxy")
	artifactCanary := flag.Bool("artifact-canary", false, "run the default-off S3 artifact canary")
	version := flag.Bool("version", false, "print immutable artifact provenance")
	flag.Parse()
	if boolCount(*version, *mtlsProxy, *artifactCanary) > 1 {
		fmt.Fprintln(os.Stderr, "coding executor scorer mode is ambiguous")
		os.Exit(64)
	}
	if *version {
		if err := json.NewEncoder(os.Stdout).Encode(release.Resolve(os.Getenv)); err != nil {
			fmt.Fprintln(os.Stderr, "coding executor scorer cannot report version")
			os.Exit(111)
		}
		return
	}
	if *mtlsProxy {
		config, err := mtlsConfigurationFromEnvironment(os.Getenv)
		if err != nil {
			fmt.Fprintln(os.Stderr, "coding executor mTLS transport is not enabled")
			os.Exit(78)
		}
		if err := runMTLSProxy(config); err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintln(os.Stderr, "coding executor mTLS transport stopped")
			os.Exit(111)
		}
		return
	}
	if *artifactCanary {
		config, err := artifactCanaryConfigurationFromEnvironment(os.Getenv)
		if err != nil {
			fmt.Fprintln(os.Stderr, "coding artifact canary is not enabled")
			os.Exit(78)
		}
		if err := runArtifactCanary(config); err != nil {
			fmt.Fprintln(os.Stderr, "coding artifact canary failed")
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

func boolCount(values ...bool) int {
	count := 0
	for _, value := range values {
		if value {
			count++
		}
	}
	return count
}
