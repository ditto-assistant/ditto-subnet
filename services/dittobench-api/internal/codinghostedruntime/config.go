package codinghostedruntime

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
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
	EgressNetwork           string                      `json:"egress_network"`
	EgressProxy             string                      `json:"egress_proxy"`
	ExecutorRepository      string                      `json:"executor_repository"`
	CandidateUID            uint32                      `json:"candidate_uid"`
	CandidateGID            uint32                      `json:"candidate_gid"`
	SeccompProfile          string                      `json:"seccomp_profile"`
	AppArmorProfile         string                      `json:"apparmor_profile"`
}

type runtimeConfig struct {
	wire       configWire
	control    *codinghostedworker.ControlClient
	starts     *codingharness.HostedStartCommand
	executors  *codingexecutor.PhaseFactory
	docker     *sandbox.LocalDocker
	publicBase string
}

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
	ipText, port, err := net.SplitHostPort(wire.RouterListen)
	ip := net.ParseIP(ipText)
	portNumber, portErr := strconv.Atoi(port)
	if err != nil || ip == nil || ip.To4() == nil || !ip.IsPrivate() || ip.IsLoopback() || portErr != nil || portNumber < 1024 || portNumber > 65535 || strconv.Itoa(portNumber) != port {
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
	docker := &sandbox.LocalDocker{HarnessPort: "8080", MemoryLimit: strconv.FormatUint(p.MemoryLimitBytes, 10), TmpfsLimit: strconv.FormatUint(p.ScratchLimitBytes, 10),
		CPULimit: fmt.Sprintf("%d.%03d", p.CPUQuotaMillis/1000, p.CPUQuotaMillis%1000), PidsLimit: int(p.PidsLimit), StartTimeout: 2 * time.Minute,
		Harden: true, RequireRootless: true, RequireIsolatedDaemon: true, HostGatewayIP: ip.String(), EgressNetwork: wire.EgressNetwork, EgressProxy: wire.EgressProxy,
		SeccompProfile: wire.SeccompProfile, AppArmorProfile: wire.AppArmorProfile}
	return &runtimeConfig{wire: wire, control: control, starts: starts, executors: executors, docker: docker, publicBase: "http://host.docker.internal:" + port}, nil
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
