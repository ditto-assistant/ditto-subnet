package sandbox

import (
	"errors"
	"net"
	"net/url"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// ErrCodingRuntimeSettings refuses coding runtime settings. It never echoes a
// configured value.
var ErrCodingRuntimeSettings = errors.New("coding runtime settings are invalid")

// CodingRuntimeSettings are the coding runtime's own container settings. The
// coding host (the certification canary and the local shadow gate) never reads
// the ordinary scorer's DITTOBENCH_SANDBOX_* environment, its OpenRouter shim CA
// bundle, GITHUB_TOKEN_FILE or DITTOBENCH_ALLOW_PRIVATE_HARNESS. Each value here
// is supplied explicitly by the caller and is required unless noted.
type CodingRuntimeSettings struct {
	// DockerHost is the dedicated rootless daemon's unix:// endpoint.
	DockerHost string
	// EgressNetwork is the dedicated daemon's egress-restricted network name.
	EgressNetwork string
	// EgressProxy is http://<private IPv4>:<port>, reachable from that network.
	EgressProxy string
	// HostGatewayIP is the private IPv4 gateway that host.docker.internal names
	// inside the dedicated daemon's network namespace. It is never discovered
	// from the scorer's own interfaces.
	HostGatewayIP string
	// SeccompProfile and AppArmorProfile are optional; "unconfined" is refused.
	SeccompProfile  string
	AppArmorProfile string
}

// Fixed coding harness limits. They equal NewLocalDocker's historical defaults
// but are not read from DITTOBENCH_SANDBOX_* overrides.
const (
	codingHarnessMemoryLimit = "3g"
	codingHarnessTmpfsLimit  = "512m"
	codingHarnessCPULimit    = "2"
	codingHarnessPidsLimit   = 512
	codingHarnessStart       = 120 * time.Second
	codingHarnessBuild       = 25 * time.Minute
)

// NewCodingRuntimeDocker returns a LocalDocker built only from settings. It
// always hardens the container, requires a rootless isolated daemon, mounts no
// CA bundle, injects no TLS trust variables, carries no GitHub token and keeps
// the private-harness SSRF guard on.
func NewCodingRuntimeDocker(settings CodingRuntimeSettings) (*LocalDocker, error) {
	if !validCodingDockerHost(settings.DockerHost) || !validCodingNetworkName(settings.EgressNetwork) ||
		!ValidCodingEgressProxy(settings.EgressProxy) || !validCodingGateway(settings.HostGatewayIP) ||
		!validCodingConfinement(settings.SeccompProfile) || !validCodingConfinement(settings.AppArmorProfile) {
		return nil, ErrCodingRuntimeSettings
	}
	return &LocalDocker{
		HarnessPort:  "8080",
		MemoryLimit:  codingHarnessMemoryLimit,
		TmpfsLimit:   codingHarnessTmpfsLimit,
		CPULimit:     codingHarnessCPULimit,
		BuildTimeout: codingHarnessBuild,
		StartTimeout: codingHarnessStart,
		PidsLimit:    codingHarnessPidsLimit,
		Harden:       true,
		// No GitHubTokenFile, AllowPrivate or OpenRouterShimCABundleHostPath.
		SeccompProfile:        settings.SeccompProfile,
		AppArmorProfile:       settings.AppArmorProfile,
		RequireRootless:       true,
		RequireIsolatedDaemon: true,
		HostGatewayIP:         settings.HostGatewayIP,
		EgressNetwork:         settings.EgressNetwork,
		EgressProxy:           settings.EgressProxy,
		DockerHost:            settings.DockerHost,
	}, nil
}

func validCodingDockerHost(value string) bool {
	path, found := strings.CutPrefix(value, "unix://")
	return found && len(path) > 1 && len(path) <= 107 && filepath.IsAbs(path) && filepath.Clean(path) == path
}

func validCodingNetworkName(value string) bool {
	if value == "" || len(value) > 128 || strings.HasPrefix(value, "-") {
		return false
	}
	return !strings.ContainsFunc(value, func(r rune) bool {
		return !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-' || r == '_')
	})
}

// ValidCodingEgressProxy accepts exactly http://<private IPv4>:<port>.
func ValidCodingEgressProxy(value string) bool {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "http" || parsed.User != nil || parsed.Opaque != "" ||
		parsed.RawQuery != "" || parsed.ForceQuery || parsed.Fragment != "" || parsed.Path != "" {
		return false
	}
	port, err := strconv.Atoi(parsed.Port())
	if err != nil || port < 1 || port > 65535 || strconv.Itoa(port) != parsed.Port() {
		return false
	}
	return validCodingGateway(parsed.Hostname()) && parsed.String() == value
}

func validCodingGateway(value string) bool {
	ip := net.ParseIP(value)
	return ip != nil && ip.To4() != nil && ip.String() == value && ip.IsPrivate() && !ip.IsLoopback()
}

func validCodingConfinement(value string) bool {
	if value == "" {
		return true
	}
	return len(value) <= 256 && !strings.EqualFold(value, "unconfined") &&
		!strings.ContainsFunc(value, func(r rune) bool { return r <= ' ' || r == ',' || r == '/' || r == '\\' || r == 0x7f })
}

// dedicatedDockerEnvironment returns the Docker CLI environment for an
// explicit dedicated endpoint: the process environment without any inherited
// DOCKER_* selector (context, TLS, config directory, API version) or proxy
// variable, plus exactly DOCKER_HOST=host.
func dedicatedDockerEnvironment(environ []string, host string) []string {
	out := make([]string, 0, len(environ)+1)
	for _, entry := range environ {
		name, _, _ := strings.Cut(entry, "=")
		if inheritedDockerSelector(name) {
			continue
		}
		out = append(out, entry)
	}
	return append(out, "DOCKER_HOST="+host)
}

func inheritedDockerSelector(name string) bool {
	upper := strings.ToUpper(name)
	switch upper {
	case "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY":
		return true
	}
	return strings.HasPrefix(upper, "DOCKER_")
}
