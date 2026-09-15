package codinghostedruntime

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedinput"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedworker"
	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

var (
	ErrConfig    = errors.New("hosted runtime configuration rejected")
	ErrConsumed  = errors.New("hosted runtime directory consumed; reconciliation required")
	ErrExecution = errors.New("hosted runtime did not finalize; reconciliation required")
	ErrCleanup   = errors.New("hosted runtime cleanup unconfirmed; operator recovery required")
)

// Harness uses the existing Go field names; Expected and profiles retain their
// existing wire shapes. None of this private input is a public API or receipt.
type configWire struct {
	Schema                  string                      `json:"schema"`
	ShadowOnly              *bool                       `json:"shadow_only"`
	WeightEligible          *bool                       `json:"weight_eligible"`
	Expected                codinghostedinput.Expected  `json:"expected"`
	Harness                 codingharness.HostedBinding `json:"harness"`
	AuthoringProfileFile    string                      `json:"authoring_profile_file"`
	GradingProfileFile      string                      `json:"grading_profile_file"`
	GradingProfileSHA256    string                      `json:"grading_profile_sha256"`
	ControlSocket           string                      `json:"control_socket"`
	ControlTokenFile        string                      `json:"control_token_file"`
	PythonExecutable        string                      `json:"python_executable"`
	PostgresEnvironmentFile string                      `json:"postgres_environment_file"`
	StateRoot               string                      `json:"state_root"`
	DockerExecutable        string                      `json:"docker_executable"`
	DockerSocket            string                      `json:"docker_socket"`
	RouterListen            string                      `json:"router_listen"`
	RouterNamespace         string                      `json:"router_namespace"`
	RouterExpiresAtUnix     int64                       `json:"router_expires_at_unix"`
	EgressNetwork           string                      `json:"egress_network"`
	EgressProxy             string                      `json:"egress_proxy"`
	ExecutorRepository      string                      `json:"executor_repository"`
	CandidateUID            uint32                      `json:"candidate_uid"`
	CandidateGID            uint32                      `json:"candidate_gid"`
	SeccompProfile          string                      `json:"seccomp_profile"`
	AppArmorProfile         string                      `json:"apparmor_profile"`
}

const (
	// routerNamespaceHost is the existing listener in the worker's own network
	// namespace. An omitted router_namespace keeps this behavior.
	routerNamespaceHost = "host"
	// routerNamespaceRootless creates the listener on the rootless daemon's
	// default bridge gateway inside RootlessKit's network namespace.
	routerNamespaceRootless = "rootless-netns"
)

type runtimeConfig struct {
	wire       configWire
	control    *codinghostedworker.ControlClient
	starts     *codingharness.HostedStartCommand
	executors  *codingexecutor.PhaseFactory
	docker     *sandbox.LocalDocker
	publicBase string
	// router is set only in rootless-netns mode.
	router *rootlessRouter
}

type rootlessRouter struct {
	address netip.AddrPort
	helper  string
	// expires is min(router_expires_at_unix, attempt deadline). Candidate
	// access through the in-namespace listener ends then.
	expires time.Time
}

// maxRouterAuthority matches the connectivity profile's longest window.
const maxRouterAuthority = 24 * time.Hour

func privateJSON(path string, maximum int64, value any) ([]byte, error) {
	body, err := readPrivate(path, maximum)
	if err != nil {
		return nil, ErrConfig
	}
	if codingcontract.ValidateJSONDocument(body, int(maximum)) != nil || json.Unmarshal(body, value) != nil {
		clear(body)
		return nil, ErrConfig
	}
	return body, nil
}

func loadConfig(path string) (*runtimeConfig, error) {
	return loadConfigChecked(path, protectedExecutable)
}

// Private seam for filesystem tests; production always uses protectedExecutable.
func loadConfigChecked(path string, executable func(string) bool) (*runtimeConfig, error) {
	var wire configWire
	body, err := privateJSON(path, 64<<10, &wire)
	if err != nil {
		return nil, ErrConfig
	}
	// Catalog index zero is valid, so a zero-value struct alone cannot reject
	// an omitted or null known field. All other Expected fields are nonzero.
	var presence struct {
		Expected struct {
			CatalogIndex *int `json:"catalog_index"`
		} `json:"expected"`
	}
	if json.Unmarshal(body, &presence) != nil || presence.Expected.CatalogIndex == nil {
		clear(body)
		return nil, ErrConfig
	}
	clear(body)
	if wire.Schema != "dittobench-coding-hosted-runtime-v2" || wire.ShadowOnly == nil || !*wire.ShadowOnly || wire.WeightEligible == nil || *wire.WeightEligible ||
		wire.Expected.Validate() != nil || wire.Harness.Validate(time.Now()) != nil || !privateDirectory(wire.StateRoot) {
		return nil, ErrConfig
	}
	e, h := wire.Expected, wire.Harness
	if e.EvaluationID != h.EvaluationID || e.AttemptID != h.AttemptID || e.WorkerID != h.WorkerID || e.AssignmentSHA256 != h.AssignmentSHA256 || e.DeadlineUnix != h.Deadline.Unix() {
		return nil, ErrConfig
	}
	var profile codinghostedinput.Profile
	body, err = privateJSON(wire.AuthoringProfileFile, 64<<10, &profile)
	if err != nil {
		return nil, ErrConfig
	}
	clear(body)
	profileSHA, err := codinghostedinput.ProfileDigest(profile)
	if err != nil || profileSHA != e.ExecutionProfileSHA256 || profile.Budgets.Validate() != nil || profile.ResourcePolicy.CandidateLimits.MaxPatchBytes != e.MaxPatchBytes {
		return nil, ErrConfig
	}
	var grading codinghostedworker.GradingProfile
	gradingBytes, err := privateJSON(wire.GradingProfileFile, 64<<10, &grading)
	if err != nil {
		return nil, ErrConfig
	}
	defer clear(gradingBytes)
	if codingcontract.RequireExactCanonicalJSON(gradingBytes) != nil || fmt.Sprintf("%x", sha256.Sum256(gradingBytes)) != wire.GradingProfileSHA256 || grading.Validate() != nil {
		return nil, ErrConfig
	}
	token, err := readPrivate(wire.ControlTokenFile, 32)
	if err != nil {
		return nil, ErrConfig
	}
	defer clear(token)
	control, err := codinghostedworker.NewControlClient(codinghostedworker.ControlConfig{SocketPath: wire.ControlSocket, Token: token, Expected: e, Profile: profile, GradingProfile: gradingBytes, GradingProfileSHA256: wire.GradingProfileSHA256})
	if err != nil {
		return nil, ErrConfig
	}
	var postgres []string
	body, err = privateJSON(wire.PostgresEnvironmentFile, 128<<10, &postgres)
	if err != nil {
		return nil, ErrConfig
	}
	clear(body)
	if !executable(wire.PythonExecutable) || !executable(wire.DockerExecutable) || filepath.Base(wire.DockerExecutable) != "docker" {
		return nil, ErrConfig
	}
	starts, err := codingharness.NewHostedStartCommand(codingharness.HostedStartCommandConfig{PythonExecutable: wire.PythonExecutable, PostgresEnvironment: postgres, WorkerID: e.WorkerID})
	if err != nil {
		return nil, ErrConfig
	}
	// The Docker CLI gets only an explicit local daemon. No contexts, TLS,
	// credential helpers, proxy overrides or ambient fallback are inherited.
	if !filepath.IsAbs(wire.DockerSocket) || filepath.Clean(wire.DockerSocket) != wire.DockerSocket {
		return nil, ErrConfig
	}
	info, err := os.Lstat(wire.DockerSocket)
	real, realErr := filepath.EvalSymlinks(wire.DockerSocket)
	if err != nil || realErr != nil || real != wire.DockerSocket || info.Mode()&os.ModeSocket == 0 {
		return nil, ErrConfig
	}
	owner, ok := info.Sys().(*syscall.Stat_t)
	if !ok || owner.Uid != uint32(os.Geteuid()) || info.Mode().Perm() != 0600 || !privateDirectory(filepath.Dir(wire.DockerSocket)) {
		return nil, ErrConfig
	}
	// One parse and one address policy for both namespaces: the exact canonical
	// text of a private, non-loopback IPv4 address with an unprivileged port.
	routerAddress, err := netip.ParseAddrPort(wire.RouterListen)
	if err != nil || routerAddress.String() != wire.RouterListen || !rootlessnetns.ValidAddress(routerAddress) {
		return nil, ErrConfig
	}
	var router *rootlessRouter
	switch wire.RouterNamespace {
	case "", routerNamespaceHost:
		// Host nftables bound this listener's candidate traffic; the field
		// belongs only to the rootless-netns window below.
		if wire.RouterExpiresAtUnix != 0 {
			return nil, ErrConfig
		}
	case routerNamespaceRootless:
		// The helper is bound to the installed worker's own bundle directory; no
		// configured path can select another executable.
		worker, workerErr := os.Executable()
		helper := filepath.Join(filepath.Dir(worker), rootlessnetns.HelperExecutableName)
		if workerErr != nil || !filepath.IsAbs(worker) || !executable(helper) || !executable(rootlessnetns.NsenterExecutable) {
			return nil, ErrConfig
		}
		// router_expires_at_unix is the connectivity profile's expires_at_unix,
		// which host nftables can no longer enforce for this traffic.
		now := time.Now()
		expires := time.Unix(wire.RouterExpiresAtUnix, 0)
		if wire.RouterExpiresAtUnix <= 0 || !expires.After(now) || expires.After(now.Add(maxRouterAuthority)) {
			return nil, ErrConfig
		}
		if h.Deadline.Before(expires) {
			expires = h.Deadline
		}
		router = &rootlessRouter{address: routerAddress, helper: helper, expires: expires}
	default:
		return nil, ErrConfig
	}
	proxy, err := url.Parse(wire.EgressProxy)
	if err != nil || proxy.Scheme != "http" || proxy.User != nil || proxy.RawQuery != "" || proxy.Fragment != "" || proxy.Path != "" || proxy.Port() == "" {
		return nil, ErrConfig
	}
	proxyIP := net.ParseIP(proxy.Hostname())
	proxyPort, err := strconv.Atoi(proxy.Port())
	if proxyIP == nil || !proxyIP.IsPrivate() || proxyIP.IsLoopback() || err != nil || proxyPort < 1 || proxyPort > 65535 || strconv.Itoa(proxyPort) != proxy.Port() || !identifier(wire.EgressNetwork) {
		return nil, ErrConfig
	}
	executors, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{ImageRepository: wire.ExecutorRepository, CandidateUID: wire.CandidateUID, CandidateGID: wire.CandidateGID, RequireRootless: true, RequireIsolatedDaemon: true, SeccompProfile: wire.SeccompProfile, AppArmorProfile: wire.AppArmorProfile})
	if err != nil {
		return nil, ErrConfig
	}
	for _, confinement := range []string{wire.SeccompProfile, wire.AppArmorProfile} {
		if strings.EqualFold(strings.TrimSpace(confinement), "unconfined") {
			return nil, ErrConfig
		}
	}
	p := profile.ResourcePolicy
	// Swap equals memory, so the harness has no swap, and --pull never: both
	// apply only to hosted-v2, never to the shared sandbox.
	memory := strconv.FormatUint(p.MemoryLimitBytes, 10)
	docker := &sandbox.LocalDocker{HarnessPort: "8080", MemoryLimit: memory, MemorySwapLimit: memory, PullNever: true, TmpfsLimit: strconv.FormatUint(p.ScratchLimitBytes, 10),
		CPULimit: fmt.Sprintf("%d.%03d", p.CPUQuotaMillis/1000, p.CPUQuotaMillis%1000), PidsLimit: int(p.PidsLimit), StartTimeout: 2 * time.Minute,
		Harden: true, RequireRootless: true, RequireIsolatedDaemon: true, HostGatewayIP: routerAddress.Addr().String(), EgressNetwork: wire.EgressNetwork, EgressProxy: wire.EgressProxy,
		SeccompProfile: wire.SeccompProfile, AppArmorProfile: wire.AppArmorProfile}
	return &runtimeConfig{wire: wire, control: control, starts: starts, executors: executors, docker: docker, publicBase: "http://host.docker.internal:" + strconv.Itoa(int(routerAddress.Port())), router: router}, nil
}

func identifier(s string) bool {
	if len(s) == 0 || len(s) > 128 || strings.HasPrefix(s, "-") {
		return false
	}
	return !strings.ContainsFunc(s, func(r rune) bool { return !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-' || r == '_') })
}

func (*runtimeConfig) String() string               { return "HostedRuntimeConfig{private}" }
func (c *runtimeConfig) GoString() string           { return c.String() }
func (c *runtimeConfig) LogValue() slog.Value       { return slog.StringValue(c.String()) }
func (*runtimeConfig) MarshalJSON() ([]byte, error) { return nil, ErrConfig }
func (configWire) String() string                   { return "HostedRuntimeInput{private}" }
func (c configWire) GoString() string               { return c.String() }
func (c configWire) LogValue() slog.Value           { return slog.StringValue(c.String()) }
func (configWire) MarshalJSON() ([]byte, error)     { return nil, ErrConfig }
