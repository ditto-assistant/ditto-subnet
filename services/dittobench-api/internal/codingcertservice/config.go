// Package codingcertservice is the default-off host certification service for
// the contract-v1 public coding canary. It runs as a dedicated non-root user
// beside that user's rootless Docker daemon, serves the router listener from
// inside RootlessKit's network namespace, and exposes only the canary and its
// readiness probe to the validator over one Unix socket at a fixed path with a
// pinned owner, group and mode. It has no TCP control listener.
package codingcertservice

import (
	"errors"
	"net/netip"
	"regexp"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

// Fixed host layout. None of these paths is operator-selectable.
const (
	// ControlSocketDirectory is created by systemd (RuntimeDirectory) as the
	// service user with the control group and mode 0750, so only the service
	// user can create, replace or remove entries in it.
	ControlSocketDirectory = "/run/ditto-coding-certification"
	ControlSocketPath      = ControlSocketDirectory + "/control.sock"
	ControlSocketName      = "control.sock"
	ControlSocketMode      = 0o660
	ControlDirectoryMode   = 0o750

	// DockerSocketDirectory holds the dedicated rootless daemon's socket, owned
	// by the service user with modes 0700 (directory) and 0600 (socket).
	DockerSocketDirectory = "/run/ditto-coding-certification-docker"
	DockerSocketPath      = DockerSocketDirectory + "/docker.sock"
	DockerSocketName      = "docker.sock"
	DockerSocketMode      = 0o600
	DockerDirectoryMode   = 0o700

	InstallRoot       = "/usr/local/lib/ditto-coding-certification"
	RouterHelperPath  = InstallRoot + "/" + rootlessnetns.HelperExecutableName
	CertificationRoot = InstallRoot + "/certification-root"
	PolicyPath        = InstallRoot + "/coding_inference_policy_locked_v1.json"
	StateRoot         = "/var/lib/ditto-coding-certification"
	PrivateRoot       = StateRoot + "/private"

	// ControlTokenCredential is read from $CREDENTIALS_DIRECTORY (systemd
	// LoadCredential). It is distinct from the Compose scorer's bearer.
	ControlTokenCredential = "control-token"
	CandidateUID           = 65532
	CandidateGID           = 65532
)

// Environment names. The service reads nothing else from its environment:
// never the ordinary scorer's sandbox settings, DOCKER_HOST, a CA bundle or a
// proxy variable.
const (
	EnabledEnvironment           = "DITTOBENCH_CODING_CERTIFICATION_SERVICE_ENABLED"
	ServiceUIDEnvironment        = "DITTOBENCH_CODING_CERTIFICATION_SERVICE_UID"
	ControlGIDEnvironment        = "DITTOBENCH_CODING_CERTIFICATION_CONTROL_GID"
	RouterListenEnvironment      = "DITTOBENCH_CODING_CERTIFICATION_ROUTER_LISTEN"
	EgressNetworkEnvironment     = "DITTOBENCH_CODING_CERTIFICATION_EGRESS_NETWORK"
	EgressProxyEnvironment       = "DITTOBENCH_CODING_CERTIFICATION_EGRESS_PROXY"
	SeccompEnvironment           = "DITTOBENCH_CODING_CERTIFICATION_SECCOMP_PROFILE"
	AppArmorEnvironment          = "DITTOBENCH_CODING_CERTIFICATION_APPARMOR_PROFILE"
	RuntimeRepositoryEnvironment = "DITTOBENCH_CODING_CERTIFICATION_RUNTIME_IMAGE_REPOSITORY"
	RuntimeDigestEnvironment     = "DITTOBENCH_CODING_CERTIFICATION_RUNTIME_IMAGE_DIGEST"
	PackManifestEnvironment      = "DITTOBENCH_CODING_CERTIFICATION_PACK_MANIFEST_SHA256"
	RouterHelperEnvironment      = "DITTOBENCH_CODING_CERTIFICATION_ROUTER_HELPER_SHA256"
)

var (
	// ErrDisabled is the default: the explicit enable flag is not exactly "true".
	ErrDisabled = errors.New("coding certification service is disabled")
	// ErrConfig refuses a configuration without echoing any configured value.
	ErrConfig = errors.New("coding certification service configuration is invalid")

	imageDigestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	sha256Pattern      = regexp.MustCompile(`^[0-9a-f]{64}$`)
	// repositoryPattern is the lowercase Docker repository grammar shared with
	// codingexecutor and the validator_stack role.
	repositoryPattern = regexp.MustCompile(`^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?/)?[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$`)
)

// Config is the validated service configuration. It holds no secret.
type Config struct {
	ServiceUID          int
	ControlGID          int
	RouterListen        netip.AddrPort
	Runtime             sandbox.CodingRuntimeSettings
	RuntimeRepository   string
	RuntimeImageDigest  string
	PackManifestSHA256  string
	RouterHelperSHA256  string
	CertificationRoot   string
	ControlSocketPath   string
	DockerSocketPath    string
	RouterHelperPath    string
	PrivateRoot         string
	PolicyPath          string
	CredentialDirectory string
}

// ConfigFromEnvironment returns ErrDisabled unless the enable flag is exactly
// "true", and ErrConfig for any missing or invalid value. euid must be the
// configured non-root service user.
func ConfigFromEnvironment(getenv func(string) string, euid int) (Config, error) {
	if getenv == nil || getenv(EnabledEnvironment) != "true" {
		return Config{}, ErrDisabled
	}
	uid, uidErr := strictID(getenv(ServiceUIDEnvironment))
	gid, gidErr := strictID(getenv(ControlGIDEnvironment))
	if uidErr != nil || gidErr != nil || euid <= 0 || uid != euid {
		return Config{}, ErrConfig
	}
	router, err := netip.ParseAddrPort(getenv(RouterListenEnvironment))
	if err != nil || router.String() != getenv(RouterListenEnvironment) || !rootlessnetns.ValidAddress(router) {
		return Config{}, ErrConfig
	}
	runtime := sandbox.CodingRuntimeSettings{
		DockerHost:      "unix://" + DockerSocketPath,
		EgressNetwork:   getenv(EgressNetworkEnvironment),
		EgressProxy:     getenv(EgressProxyEnvironment),
		HostGatewayIP:   router.Addr().String(),
		SeccompProfile:  getenv(SeccompEnvironment),
		AppArmorProfile: getenv(AppArmorEnvironment),
	}
	if _, err := sandbox.NewCodingRuntimeDocker(runtime); err != nil {
		return Config{}, ErrConfig
	}
	repository := getenv(RuntimeRepositoryEnvironment)
	digest := getenv(RuntimeDigestEnvironment)
	manifest := getenv(PackManifestEnvironment)
	helper := getenv(RouterHelperEnvironment)
	if len(repository) > 255 || !repositoryPattern.MatchString(repository) ||
		!imageDigestPattern.MatchString(digest) || !sha256Pattern.MatchString(manifest) ||
		!sha256Pattern.MatchString(helper) {
		return Config{}, ErrConfig
	}
	credentials := getenv("CREDENTIALS_DIRECTORY")
	if !strings.HasPrefix(credentials, "/run/credentials/") || strings.Contains(credentials, "..") ||
		strings.ContainsAny(credentials, "\x00\n") {
		return Config{}, ErrConfig
	}
	return Config{
		ServiceUID: uid, ControlGID: gid, RouterListen: router, Runtime: runtime,
		RuntimeRepository: repository, RuntimeImageDigest: digest, PackManifestSHA256: manifest,
		RouterHelperSHA256: helper,
		CertificationRoot:  CertificationRoot, ControlSocketPath: ControlSocketPath,
		DockerSocketPath: DockerSocketPath, RouterHelperPath: RouterHelperPath,
		PrivateRoot: PrivateRoot, PolicyPath: PolicyPath, CredentialDirectory: credentials,
	}, nil
}

// strictID accepts a canonical decimal ID in [1, 2^31-2]. Zero (root) and the
// overflow/nobody sentinels are refused.
func strictID(value string) (int, error) {
	parsed, err := strconv.Atoi(value)
	if err != nil || strconv.Itoa(parsed) != value || parsed < 1 || parsed > 1<<31-2 {
		return 0, ErrConfig
	}
	return parsed, nil
}

func (config Config) routerConfig() rootlessnetns.Config {
	return rootlessnetns.Config{
		Address: config.RouterListen, DockerSocket: config.DockerSocketPath, HelperExecutable: config.RouterHelperPath,
	}
}
