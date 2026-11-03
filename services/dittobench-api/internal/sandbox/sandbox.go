// Package sandbox loads and runs an untrusted miner harness in an isolated
// container. Active V7/V8 validator work uses only the exact image produced by
// the trusted screener; local development retains a source-build path.
//
// The Sandbox interface lets the API swap execution backends without touching
// the submit handler:
//
//   - LocalDocker (this file): screened-image load + `docker run` on a dedicated
//     rootless daemon, with source builds retained for local development.
//   - CloudBuild (future, for the Cloud Run deployment): Cloud Build to build
//     the image + a Cloud Run Job to run it. Cloud Run has no local Docker
//     daemon, so the LocalDocker backend cannot run there; see README.
//
// Isolation: resource caps (memory/cpu/pids) + auto-remove + no-new-privileges,
// plus opt-in hardening for untrusted (on-chain) submissions — `--cap-drop ALL`
// (Harden) and an egress allowlist via a restricted network + forward proxy
// (EgressNetwork/EgressProxy; see docs/sandbox-egress.md, the egress proof).
// Production requires either an operator-owned rootless
// endpoint or the release-owned, credential-empty nested daemon used by the
// managed stack; a host rootful Docker socket remains explicitly incompatible
// with the hardened boundary.
package sandbox

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/astfp"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const (
	// OpenRouterShimCABundlePath is the read-only path mounted into an untrusted
	// harness when the validator enables the hardcoded-OpenRouter compatibility
	// shim. It contains public roots plus the validator-local shim CA; no private
	// key ever enters the sandbox.
	OpenRouterShimCABundlePath = "/run/dittobench/openrouter-shim-ca.pem"
	openRouterShimHost         = "openrouter.ai"
	brokerCapabilityHostSuffix = ".host.docker.internal"
	isolatedDaemonLabel        = "io.heyditto.dittobench.isolated=true"
)

// Sandbox builds a submission into a runnable image and runs it as an
// addressable harness serving the /run + /health contract.
type Sandbox interface {
	// Available returns nil if the backend can build and run images.
	Available(ctx context.Context) error
	// Build clones src (a git URL at ref) and builds it into an image,
	// returning the image reference and the tail of the build log.
	Build(ctx context.Context, src Source) (image string, buildLog string, fingerprint *protocol.CodeFingerprint, err error)
	// Run starts image as a detached container exposing the harness port and
	// returns a handle. Caller must Stop the handle.
	Run(ctx context.Context, image string, env map[string]string) (*Handle, error)
	// Release removes a request-scoped image returned by Build. It is safe to
	// call more than once and must also be called when work fails before Run.
	Release(ctx context.Context, image string)
	// Stop force-removes the container behind h. Safe to call more than once.
	Stop(ctx context.Context, h *Handle)
	// StopRetainingImage force-removes and verifies the container behind h but
	// retains its built image for another isolated run. It returns an error when
	// container removal cannot be proven.
	StopRetainingImage(ctx context.Context, h *Handle) error
	// Diagnostics captures sanitized container resource evidence before Stop.
	// It never returns miner output, environment variables, paths, or source.
	Diagnostics(ctx context.Context, h *Handle) RuntimeDiagnostics
	// Logs returns a bounded, redacted tail of the container's merged
	// stdout+stderr, captured before Stop removes the container. Unlike
	// Diagnostics this DOES return miner output -- that is the point: it is the
	// harness's own boot failure, and it is the only evidence that explains why
	// a container exited before health. Empty when the container produced
	// nothing or the runtime could not be queried.
	Logs(ctx context.Context, h *Handle) string
}

// Source identifies a submission to build. Exactly one of GitURL or TarballURL
// is set: GitURL clones a repo; TarballURL fetches a presigned gzipped-tar of
// the harness (the SN118 platform stores miner uploads as tarballs, so the
// validator hands us the platform's short-lived download URL).
type Source struct {
	GitURL     string // e.g. https://github.com/<miner>/<harness>
	GitRef     string // required full lowercase commit SHA; never a mutable branch or tag
	GitSubdir  string // optional repository-relative Docker context
	TarballURL string // presigned https URL of a gzipped tar of the harness
	// TarballSHA256, when non-empty, is verified (hex) against the fetched bytes.
	// The platform already checks it at upload; re-verifying makes the sandbox
	// self-defending against a corrupted or swapped blob behind the URL.
	TarballSHA256       string
	ScreenedImageURL    string
	ScreenedImageSHA256 string
	ScreenedImageID     string
	ScreenedImageRef    string
	ScreenedImageSize   int64
}

func resolveGitContext(workdir, subdir string) (string, error) {
	root, err := filepath.EvalSymlinks(workdir)
	if err != nil {
		return "", fmt.Errorf("resolve git checkout: %w", err)
	}
	trimmed := strings.TrimSpace(subdir)
	if trimmed == "" || trimmed == "." {
		return root, nil
	}
	clean := filepath.Clean(filepath.FromSlash(trimmed))
	if filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("git_subdir must be a repository-relative path")
	}
	candidate, err := filepath.EvalSymlinks(filepath.Join(root, clean))
	if err != nil {
		return "", fmt.Errorf("resolve git_subdir: %w", err)
	}
	relative, err := filepath.Rel(root, candidate)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("git_subdir escapes the repository checkout")
	}
	info, err := os.Stat(candidate)
	if err != nil {
		return "", fmt.Errorf("stat git_subdir: %w", err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("git_subdir is not a directory")
	}
	return candidate, nil
}

// Handle references a running harness container.
type Handle struct {
	ContainerID string
	BaseURL     string // e.g. http://127.0.0.1:49160 — pass to runner.RunHarness
	// ImageRef is the request-scoped image created by Build. Stop removes it
	// after removing the container so long-lived validators do not accumulate
	// multi-gigabyte submission images.
	ImageRef string
	// NetworkName is the request-private bridge created for this container.
	// Stop removes it after removing the sole untrusted member.
	NetworkName string
	SourceIP    string
	// injectedSecrets holds the credential-shaped env VALUES this run injected
	// into the container. Logs masks them, so no value the validator put into
	// the sandbox can be echoed back out of it by the harness's own output --
	// deliberately or accidentally. Unexported: it is redaction state, not part
	// of the handle's contract.
	injectedSecrets []string
	// memoryLimitBytes is the --memory cap this container runs under, the
	// denominator of the harness_oom rule. Zero when it could not be parsed.
	memoryLimitBytes uint64
	// watch samples cgroup and host memory while the container lives, so a
	// harness the kernel kills -- taking its cgroup with it -- still leaves
	// the evidence that decides whose fault the kill was.
	watch *memoryWatch
}

// RuntimeDiagnostics is bounded, source-free evidence captured before teardown.
// Pointer fields distinguish an observed zero from a runtime that could not
// expose the corresponding cgroup/tmpfs counter (for example after a hard exit).
type RuntimeDiagnostics struct {
	StateKnown         bool              `json:"-"`
	Running            bool              `json:"running"`
	OOMKilled          bool              `json:"oom_killed"`
	ExitCode           int               `json:"exit_code"`
	MemoryPeakBytes    *uint64           `json:"memory_peak_bytes,omitempty"`
	MemoryEvents       map[string]uint64 `json:"memory_events,omitempty"`
	TmpfsUsedBytes     *uint64           `json:"tmpfs_used_bytes,omitempty"`
	TmpfsCapacityBytes *uint64           `json:"tmpfs_capacity_bytes,omitempty"`
	// MemoryLimitBytes is the harness's own --memory cap.
	MemoryLimitBytes *uint64 `json:"memory_limit_bytes,omitempty"`
	// HostMemAvailableMinBytes is the lowest host MemAvailable sampled in the
	// window that ended when the container stopped (or now, if it is alive).
	HostMemAvailableMinBytes *uint64 `json:"host_mem_available_min_bytes,omitempty"`
}

// Stable resource failure codes. docs/RUNTIME-ENVELOPE.md publishes each rule.
const (
	// CodeHarnessOOM is the miner's: the harness reached its own published cap
	// while the host still had headroom.
	CodeHarnessOOM = "harness_oom"
	// CodeHostMemoryPressure is the validator's: a kill the evidence cannot
	// pin on the harness's own cap, or one taken while the host was short.
	CodeHostMemoryPressure = "host_memory_pressure"
	// CodeHarnessKilled137 is a SIGKILL with no OOM flag. Nothing proves who
	// sent it, so it is infrastructure; Platform treats a repeat on a second
	// validator as the harness's.
	CodeHarnessKilled137 = "harness_killed_137"
	// CodeTmpfsExhausted is unchanged: /tmp filled to its declared size.
	CodeTmpfsExhausted = "sandbox_tmpfs_exhausted"
)

// ResourceFailure is one deterministic verdict from sandbox evidence.
type ResourceFailure struct {
	Code       string
	MinerFault bool
}

// ResourceFailure classifies a failed run from physical evidence alone. An
// OOM is the miner's only when all three hold: the kernel flagged an OOM kill,
// the cgroup reached its own cap (memory.peak within the published tolerance
// of the limit, or memory.events counted an oom at the limit), and host
// MemAvailable stayed above the published floor up to the kill. Missing
// evidence never charges the miner.
func (d RuntimeDiagnostics) ResourceFailure() (ResourceFailure, bool) {
	if d.OOMKilled || d.MemoryEvents["oom_kill"] > 0 {
		if d.capReached() && d.hostHadHeadroom() {
			return ResourceFailure{Code: CodeHarnessOOM, MinerFault: true}, true
		}
		return ResourceFailure{Code: CodeHostMemoryPressure}, true
	}
	if d.TmpfsUsedBytes != nil && d.TmpfsCapacityBytes != nil &&
		*d.TmpfsCapacityBytes > 0 && *d.TmpfsUsedBytes >= *d.TmpfsCapacityBytes {
		return ResourceFailure{Code: CodeTmpfsExhausted}, true
	}
	if d.StateKnown && !d.Running && d.ExitCode == 128+9 {
		return ResourceFailure{Code: CodeHarnessKilled137}, true
	}
	return ResourceFailure{}, false
}

func (d RuntimeDiagnostics) capReached() bool {
	if d.MemoryLimitBytes == nil || *d.MemoryLimitBytes == 0 {
		return false
	}
	// memory.events "oom" counts only this cgroup reaching its own limit; a
	// host-wide OOM kill increments "oom_kill" without it.
	if d.MemoryEvents["oom"] > 0 {
		return true
	}
	tolerance := uint64(PublishedEnvelope.OOM.PeakToleranceMiB) * mib
	return d.MemoryPeakBytes != nil && *d.MemoryPeakBytes+tolerance >= *d.MemoryLimitBytes
}

func (d RuntimeDiagnostics) hostHadHeadroom() bool {
	floor := uint64(PublishedEnvelope.OOM.HostAvailableFloorMiB) * mib
	return d.HostMemAvailableMinBytes != nil && *d.HostMemAvailableMinBytes > floor
}

type memoryLimitKey struct{}

// WithMemoryLimitMiB returns ctx carrying a per-run sandbox memory cap. A
// Platform-issued ticket cap is the same on every validator, so it replaces
// the host's DITTOBENCH_SANDBOX_MEMORY_LIMIT default for that run only.
func WithMemoryLimitMiB(ctx context.Context, mib int) context.Context {
	if mib <= 0 {
		return ctx
	}
	return context.WithValue(ctx, memoryLimitKey{}, strconv.Itoa(mib)+"m")
}

func memoryLimitFromContext(ctx context.Context) (string, bool) {
	limit, ok := ctx.Value(memoryLimitKey{}).(string)
	return limit, ok && limit != ""
}

// illegalInstructionPattern matches a harness process killed by SIGILL: the
// supervisord exit line, the shell's report, or a bare Go/Rust runtime trace.
var illegalInstructionPattern = regexp.MustCompile(
	`terminated by SIGILL|Illegal instruction|signal: illegal instruction|SIGILL: illegal instruction`,
)

// SIGILL evidence sources, recorded on the failure so an operator can tell the
// runtime's own report from a match in miner-controlled output.
const (
	IllegalInstructionEvidenceExitCode = "exit_code"
	IllegalInstructionEvidenceLogTail  = "log_tail"
)

// StartupIllegalInstruction reports a harness that died on an instruction the
// validator host's CPU does not implement before it ever became healthy, and
// which evidence showed it. Exit code 132 is the container runtime's own report
// for PID 1. A supervisord-managed harness never exits, so its SIGILL appears
// only in the log tail, which is miner output and therefore unauthenticated. A
// forged line can only move a failed attempt into validator-owned parking that
// needs an operator grant to retry; it never produces a score.
func StartupIllegalInstruction(exitCode int, logTail string) (bool, string) {
	if exitCode == 128+4 {
		return true, IllegalInstructionEvidenceExitCode
	}
	if illegalInstructionPattern.MatchString(logTail) {
		return true, IllegalInstructionEvidenceLogTail
	}
	return false, ""
}

// LocalDocker runs submissions on the host Docker daemon via the `docker` CLI.
type LocalDocker struct {
	// HarnessPort is the in-container port the harness serves on (the kit's
	// `serve` binds 8080).
	HarnessPort string
	// MemoryLimit / CPULimit cap the container (docker --memory / --cpus).
	MemoryLimit string
	// TmpfsLimit caps the only writable filesystem mounted at /tmp.
	TmpfsLimit string
	// CPULimit is passed to docker --cpus.
	CPULimit string
	// BuildTimeout bounds a single `docker build` (cold dependency builds are slow).
	BuildTimeout time.Duration
	// StartTimeout bounds image unpack + container creation. Concurrent screened
	// image starts can exceed Docker's usual fast path without being hung.
	StartTimeout time.Duration
	// GitHubTokenFile, if set, is used only by the host-side git clone for a
	// private source repository. It is never copied or mounted into an untrusted
	// build context. Defaults from GITHUB_TOKEN_FILE.
	GitHubTokenFile string
	// AllowPrivate relaxes the SSRF guard on TarballURL fetches (local dev only,
	// e.g. a minio/localhost presigned URL). Mirrors the submit-handler flag.
	AllowPrivate bool
	// AllowSourceBuild is reserved for explicitly constructed local development
	// sandboxes. NewLocalDocker leaves it disabled, and the production scorer
	// never enables it: validator jobs must run a screener-built image.
	AllowSourceBuild bool
	// PidsLimit caps the container process/thread count (docker --pids-limit) —
	// a fork-bomb bound. Always applied; defaults to 512.
	PidsLimit int
	// Harden, when true, drops all Linux capabilities (--cap-drop ALL). An
	// untrusted userland HTTP harness needs none. Hardening is on by default;
	// DITTOBENCH_SANDBOX_HARDEN=0 is reserved for local debugging.
	Harden bool
	// SeccompProfile and AppArmorProfile are explicit runtime policy names.
	// Docker's built-in seccomp profile remains active when SeccompProfile is
	// empty. AppArmor is opt-in because it is unavailable on some hosts.
	SeccompProfile  string
	AppArmorProfile string
	// RequireRootless fails availability checks unless the selected Docker daemon
	// advertises rootless mode. Operators can provision the endpoint first, then
	// enable DITTOBENCH_REQUIRE_ROOTLESS_DOCKER without changing scoring.
	RequireRootless bool
	// RequireIsolatedDaemon fails availability checks unless the selected Docker
	// daemon advertises the release-owned isolated-daemon label. The managed
	// validator stack uses this for its credential-empty privileged DinD service;
	// untrusted containers remain confined by the DinD-local egress firewall and
	// never receive a host Docker socket.
	RequireIsolatedDaemon bool
	// HostGatewayIP is the trusted scorer/broker address visible from containers
	// owned by a nested rootless daemon. Rootless Docker cannot use the rootful
	// host-gateway magic to reach its outer network namespace, so production
	// normally discovers eth0 and may override it explicitly.
	HostGatewayIP string
	// EgressNetwork, when set, attaches the container to this user-defined docker
	// network — the egress-restricted sandbox network (allowlisting proxy + host
	// firewall) — instead of the default full-egress bridge. Empty = today's
	// behavior. Env DITTOBENCH_SANDBOX_EGRESS_NETWORK. See
	// docs/sandbox-egress.md (the egress proof).
	EgressNetwork string
	// EgressProxy, when set, is injected as HTTPS_PROXY/HTTP_PROXY so the harness's
	// outbound calls are forced through the allowlisting forward proxy (loopback +
	// the host gateway bypass it via NO_PROXY). Env DITTOBENCH_SANDBOX_EGRESS_PROXY.
	EgressProxy string
	// OpenRouterShimCABundleHostPath is the validator-owned CA bundle path as
	// seen by the nested Docker daemon. When set, hardcoded openrouter.ai resolves
	// to the host gateway and standard TLS clients trust only the additional
	// validator-local certificate mounted from this path. The corresponding TLS
	// listener remains source-bound by the inference broker.
	OpenRouterShimCABundleHostPath string
	// MemoryWatchInterval paces the per-run cgroup/host memory sampler. Zero
	// disables it (a directly constructed LocalDocker in tests).
	MemoryWatchInterval time.Duration
	// dockerCommand is injectable only for deterministic command/parse tests.
	dockerCommand func(context.Context, ...string) ([]byte, error)
	// hostMemAvailable is injectable only for tests; nil reads /proc/meminfo.
	hostMemAvailable func() (uint64, bool)
}

// NewLocalDocker returns a LocalDocker with sensible defaults.
func NewLocalDocker() *LocalDocker {
	return &LocalDocker{
		HarnessPort: "8080",
		// The historical caps (3g / 512m) remain the defaults so v2..v6 replay
		// envelopes are unchanged. A validator administering the v7 difficulty
		// suite (~10x denser haystacks: a much larger on-disk memory store and
		// embedding working set) should raise these via
		// DITTOBENCH_SANDBOX_MEMORY_LIMIT / DITTOBENCH_SANDBOX_TMPFS_LIMIT
		// (docker --memory / tmpfs size syntax, e.g. "6g" / "2g").
		MemoryLimit: envStrDefault("DITTOBENCH_SANDBOX_MEMORY_LIMIT", "3g"),
		TmpfsLimit: envStrDefault(
			"DITTOBENCH_SANDBOX_TMPFS_LIMIT",
			strconv.Itoa(PublishedEnvelope.Harness.TmpfsMiB)+"m",
		),
		CPULimit:            strconv.Itoa(PublishedEnvelope.Harness.CPUs),
		MemoryWatchInterval: defaultMemoryWatchInterval,
		BuildTimeout:        25 * time.Minute,
		StartTimeout:        time.Duration(envIntDefault("DITTOBENCH_SANDBOX_START_TIMEOUT_SECONDS", PublishedEnvelope.Timing.ContainerStartSeconds)) * time.Second,
		GitHubTokenFile:     os.Getenv("GITHUB_TOKEN_FILE"),
		AllowPrivate:        envBool("DITTOBENCH_ALLOW_PRIVATE_HARNESS"),
		PidsLimit:           envIntDefault("DITTOBENCH_SANDBOX_PIDS_LIMIT", PublishedEnvelope.Harness.PidsLimit),
		Harden:              envBoolDefault("DITTOBENCH_SANDBOX_HARDEN", true),
		SeccompProfile:      strings.TrimSpace(os.Getenv("DITTOBENCH_SANDBOX_SECCOMP_PROFILE")),
		AppArmorProfile:     strings.TrimSpace(os.Getenv("DITTOBENCH_SANDBOX_APPARMOR_PROFILE")),
		RequireRootless:     envBool("DITTOBENCH_REQUIRE_ROOTLESS_DOCKER"),
		RequireIsolatedDaemon: envBool(
			"DITTOBENCH_REQUIRE_ISOLATED_DOCKER_DAEMON",
		),
		HostGatewayIP: strings.TrimSpace(os.Getenv("DITTOBENCH_SANDBOX_HOST_GATEWAY_IP")),
		EgressNetwork: strings.TrimSpace(os.Getenv("DITTOBENCH_SANDBOX_EGRESS_NETWORK")),
		EgressProxy:   strings.TrimSpace(os.Getenv("DITTOBENCH_SANDBOX_EGRESS_PROXY")),
		OpenRouterShimCABundleHostPath: strings.TrimSpace(
			os.Getenv("DITTOBENCH_OPENROUTER_SHIM_CA_BUNDLE_PATH"),
		),
	}
}

// envBool reports whether an env var is set to a truthy value.
func envBool(name string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(name))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func envBoolDefault(name string, def bool) bool {
	value, ok := os.LookupEnv(name)
	if !ok || strings.TrimSpace(value) == "" {
		return def
	}
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return def
	}
}

// envStrDefault reads a non-empty env var, falling back to def when unset or
// blank.
func envStrDefault(name, def string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return def
}

// envIntDefault reads a positive int env var, falling back to def on unset or
// invalid/non-positive input.
func envIntDefault(name string, def int) int {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

// Available checks that Docker is reachable and satisfies operator policy.
func (d *LocalDocker) Available(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	out, err := d.dockerOutput(ctx, "info", "--format", "{{json .SecurityOptions}}")
	if err != nil {
		return fmt.Errorf("docker daemon unavailable: %s: %w", strings.TrimSpace(string(out)), err)
	}
	if d.RequireRootless {
		var options []string
		if err := json.Unmarshal(out, &options); err != nil {
			return fmt.Errorf("docker daemon unavailable: invalid security options: %w", err)
		}
		rootless := false
		for _, option := range options {
			if strings.Contains(strings.ToLower(option), "rootless") {
				rootless = true
				break
			}
		}
		if !rootless {
			return fmt.Errorf("docker daemon unavailable: configured endpoint is not rootless")
		}
		if _, err := d.sandboxHostGateway(); err != nil {
			return fmt.Errorf("docker daemon unavailable: %w", err)
		}
	}
	if d.RequireIsolatedDaemon {
		out, err := d.dockerOutput(ctx, "info", "--format", "{{json .Labels}}")
		if err != nil {
			return fmt.Errorf("docker daemon unavailable: %s: %w", strings.TrimSpace(string(out)), err)
		}
		var labels []string
		if err := json.Unmarshal(out, &labels); err != nil {
			return fmt.Errorf("docker daemon unavailable: invalid daemon labels: %w", err)
		}
		if !slices.Contains(labels, isolatedDaemonLabel) {
			return errors.New("docker daemon unavailable: configured endpoint is not the isolated validator daemon")
		}
	}
	return nil
}

// V8IsolationReady reports whether the configured executor is reachable and
// satisfies the operator-selected isolation policy. RequireRootless remains a
// fail-closed hardening option, but the reviewed privileged-DinD compatibility
// stack can serve V8 while older managed validators migrate to rootless DinD.
func (d *LocalDocker) V8IsolationReady(ctx context.Context) error {
	if err := d.Available(ctx); err != nil {
		return fmt.Errorf("v8 executor isolation unavailable: %w", err)
	}
	return nil
}

// Build materializes the submission in a temp dir, then loads the screener-built
// image. Explicit local development sandboxes may opt into building source with
// BuildKit. Validator tickets always require the exact screener-built image.
// A private source repository may use host-side askpass authentication, but no
// credential enters the build context, image, Dockerfile, or command line.
func (d *LocalDocker) Build(ctx context.Context, src Source) (string, string, *protocol.CodeFingerprint, error) {
	if src.GitURL == "" && src.TarballURL == "" && src.ScreenedImageURL == "" {
		return "", "", nil, fmt.Errorf("sandbox: one of git_url, tarball_url, or screened_image_url is required")
	}
	if src.GitURL == "" && src.TarballURL == "" {
		// Source-free validator path: only the screened image is fetched.
		// Platform computes the structural fingerprint from the trusted upload.
		image, loadLog, err := d.LoadScreenedImage(ctx, Source{
			ScreenedImageURL: src.ScreenedImageURL, ScreenedImageSHA256: src.ScreenedImageSHA256,
			ScreenedImageID: src.ScreenedImageID, ScreenedImageRef: src.ScreenedImageRef,
			ScreenedImageSize: src.ScreenedImageSize,
		})
		return image, loadLog, nil, err
	}
	if src.GitURL != "" && !ValidGitCommitSHA(src.GitRef) {
		return "", "", nil, errors.New("git_ref must be a full 40-character lowercase commit SHA")
	}
	if src.ScreenedImageURL == "" && !d.AllowSourceBuild {
		return "", "", nil, errors.New("sandbox: screened image is required; source builds are disabled")
	}
	ctx, cancel := context.WithTimeout(ctx, d.BuildTimeout)
	defer cancel()

	// 1. Materialize the submission into a throwaway working tree, either by
	//    cloning a git repo or by extracting a presigned tarball.
	workdir, err := os.MkdirTemp("", "dittobench-sub-")
	if err != nil {
		return "", "", nil, fmt.Errorf("mktemp: %w", err)
	}
	defer os.RemoveAll(workdir)

	// The docker build context: the git-clone root, or — for a tarball — the
	// extraction root or the lone top-level directory that holds the Dockerfile.
	contextDir := workdir
	if src.TarballURL != "" {
		cdir, err := d.fetchTarball(ctx, src, workdir)
		if err != nil {
			return "", "", nil, err
		}
		contextDir = cdir
	} else {
		cloneEnv, cleanupAuth, err := d.cloneEnvironment()
		if err != nil {
			return "", "", nil, err
		}
		defer cleanupAuth()
		if err := materializeGitSource(ctx, src, workdir, cloneEnv); err != nil {
			return "", "", nil, err
		}
		contextDir, err = resolveGitContext(workdir, src.GitSubdir)
		if err != nil {
			return "", "", nil, err
		}
	}

	// 1b. Structural fingerprint of the materialized harness, for the platform's
	//     anti-copy gate. Computed here (the only place the tree is unpacked and a
	//     parser is available) while contextDir still exists — the deferred
	//     RemoveAll wipes it on return. Best-effort: nil on any problem, never
	//     failing the build over a moderation signal.
	fingerprint := astfp.FromDir(ctx, contextDir)

	// 2. Load the screener-built image when present. The source tree above is
	// still materialized so validators preserve structural anti-copy evidence.
	if src.ScreenedImageURL != "" {
		image, loadLog, err := d.loadScreenedImage(ctx, src, workdir)
		if err != nil {
			return "", loadLog, nil, err
		}
		return image, loadLog, fingerprint, nil
	}

	// 3. Build the local context for legacy/evaluating records without an image.
	buildIdentity, err := isolatedIdentity()
	if err != nil {
		return "", "", nil, err
	}
	image := "dittobench-sub:" + safeTag(src) + "-" + buildIdentity
	args := []string{"build", "-t", image}
	args = append(args, contextDir)

	cmd := exec.CommandContext(ctx, "docker", args...)
	cmd.Env = append(os.Environ(), "DOCKER_BUILDKIT=1")
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	if err := cmd.Run(); err != nil {
		return "", tail(buf.String(), 4000), nil, fmt.Errorf("docker build failed: %w", err)
	}
	return image, tail(buf.String(), 2000), fingerprint, nil
}

// ValidGitCommitSHA accepts only an immutable, canonical Git SHA-1 object ID.
// A branch, tag, abbreviation, or all-zero placeholder cannot pin a build.
func ValidGitCommitSHA(ref string) bool {
	if len(ref) != 40 || ref != strings.ToLower(ref) || ref == strings.Repeat("0", 40) {
		return false
	}
	_, err := hex.DecodeString(ref)
	return err == nil
}

func materializeGitSource(ctx context.Context, src Source, workdir string, env []string) error {
	if !ValidGitCommitSHA(src.GitRef) {
		return errors.New("git_ref must be a full 40-character lowercase commit SHA")
	}
	git := func(args ...string) ([]byte, error) {
		cmd := exec.CommandContext(ctx, "git", args...)
		cmd.Env = env
		return cmd.CombinedOutput()
	}
	if out, err := git("init", "-q", workdir); err != nil {
		return fmt.Errorf("git init failed: %s: %w", strings.TrimSpace(string(out)), err)
	}
	if out, err := git("-C", workdir, "remote", "add", "origin", src.GitURL); err != nil {
		return fmt.Errorf("git remote setup failed: %s: %w", strings.TrimSpace(string(out)), err)
	}
	if _, err := git("-C", workdir, "fetch", "--no-tags", "--depth", "1", "origin", src.GitRef); err != nil {
		// Git may echo a credential-bearing URL in failure output.
		return fmt.Errorf("git fetch of pinned commit failed: %w", err)
	}
	if out, err := git("-C", workdir, "checkout", "--detach", "FETCH_HEAD"); err != nil {
		return fmt.Errorf("git checkout of pinned commit failed: %s: %w", strings.TrimSpace(string(out)), err)
	}
	return verifyGitHead(ctx, workdir, env, src.GitRef)
}

func verifyGitHead(ctx context.Context, workdir string, env []string, expected string) error {
	cmd := exec.CommandContext(ctx, "git", "-C", workdir, "rev-parse", "--verify", "HEAD")
	cmd.Env = env
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("git HEAD verification failed: %s: %w", strings.TrimSpace(string(out)), err)
	}
	if got := strings.TrimSpace(string(out)); got != expected {
		return fmt.Errorf("git HEAD %q does not match pinned git_ref %q", got, expected)
	}
	return nil
}

// cloneEnvironment returns an ephemeral askpass environment for host-side git
// authentication. The helper reads the token from its process environment, so
// neither the token nor a token-bearing URL appears in argv, .git/config, the
// Docker build context, or a layer. The caller always invokes cleanup.
func (d *LocalDocker) cloneEnvironment() ([]string, func(), error) {
	if d.GitHubTokenFile == "" {
		return append(os.Environ(), "GIT_TERMINAL_PROMPT=0"), func() {}, nil
	}
	tok, err := os.ReadFile(d.GitHubTokenFile)
	if err != nil {
		return nil, func() {}, fmt.Errorf("read github token file: %w", err)
	}
	token := strings.TrimSpace(string(tok))
	if token == "" {
		return append(os.Environ(), "GIT_TERMINAL_PROMPT=0"), func() {}, nil
	}
	helper, err := os.CreateTemp("", "dittobench-git-askpass-")
	if err != nil {
		return nil, func() {}, fmt.Errorf("create git askpass helper: %w", err)
	}
	path := helper.Name()
	cleanup := func() { _ = os.Remove(path) }
	const script = `#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *) printf '%s\n' "$DITTO_GITHUB_TOKEN" ;;
esac
`
	if _, err := helper.WriteString(script); err != nil {
		_ = helper.Close()
		cleanup()
		return nil, func() {}, fmt.Errorf("write git askpass helper: %w", err)
	}
	if err := helper.Close(); err != nil {
		cleanup()
		return nil, func() {}, fmt.Errorf("close git askpass helper: %w", err)
	}
	if err := os.Chmod(path, 0o700); err != nil {
		cleanup()
		return nil, func() {}, fmt.Errorf("protect git askpass helper: %w", err)
	}
	env := append(os.Environ(),
		"GIT_TERMINAL_PROMPT=0",
		"GIT_ASKPASS="+path,
		"DITTO_GITHUB_TOKEN="+token,
	)
	return env, cleanup, nil
}

// pidsLimit returns the configured --pids-limit, defaulting to 512 so a
// zero-value LocalDocker (e.g. constructed directly in a test) is still bounded
// rather than unlimited (docker treats --pids-limit 0 as unlimited).
func (d *LocalDocker) pidsLimit() int {
	if d.PidsLimit > 0 {
		return d.PidsLimit
	}
	return 512
}

func (d *LocalDocker) tmpfsLimit() string {
	if strings.TrimSpace(d.TmpfsLimit) != "" {
		return d.TmpfsLimit
	}
	return "512m"
}

func (d *LocalDocker) startTimeout() time.Duration {
	if d.StartTimeout > 0 {
		return d.StartTimeout
	}
	return 2 * time.Minute
}

func (d *LocalDocker) dockerOutput(ctx context.Context, args ...string) ([]byte, error) {
	if d.dockerCommand != nil {
		return d.dockerCommand(ctx, args...)
	}
	return exec.CommandContext(ctx, "docker", args...).CombinedOutput()
}

// CleanupStale removes only resources carrying this scorer's ownership label.
// A process restart loses all in-memory run and broker state, so retaining an
// old untrusted container could only create cross-run interference. Explicit
// ids from Docker are used instead of names, globs, or broad prune operations.
func (d *LocalDocker) CleanupStale(ctx context.Context) error {
	const ownership = "label=io.heyditto.dittobench.run"
	containers, err := d.dockerOutput(ctx, "ps", "-aq", "--filter", ownership)
	if err != nil {
		return fmt.Errorf("list stale scorer containers: %w", err)
	}
	ids := strings.Fields(string(containers))
	if len(ids) > 0 {
		args := append([]string{"rm", "-f"}, ids...)
		if out, removeErr := d.dockerOutput(ctx, args...); removeErr != nil {
			return fmt.Errorf("remove stale scorer containers: %s: %w", strings.TrimSpace(string(out)), removeErr)
		}
	}
	networks, err := d.dockerOutput(ctx, "network", "ls", "-q", "--filter", ownership)
	if err != nil {
		return fmt.Errorf("list stale scorer networks: %w", err)
	}
	for _, id := range strings.Fields(string(networks)) {
		if out, removeErr := d.dockerOutput(ctx, "network", "rm", id); removeErr != nil {
			return fmt.Errorf("remove stale scorer network: %s: %w", strings.TrimSpace(string(out)), removeErr)
		}
	}
	return nil
}

// runArgs builds the `docker run` argument vector. Extracted from Run so the
// isolation/egress flags can be unit-tested without a live docker daemon.
func (d *LocalDocker) runArgs(image string, env map[string]string) []string {
	return d.runArgsForNetwork(image, env, d.EgressNetwork, "")
}

func (d *LocalDocker) runArgsForNetwork(image string, env map[string]string, network string, identity string) []string {
	args := []string{
		// Do not use --rm: Docker would erase an OOM-killed container before the
		// scorer can inspect State.OOMKilled/ExitCode. Every successful Run path
		// owns a deferred Stop, which removes it immediately after diagnostics.
		"run", "-d",
		"--init",
		"--user", "65532:65532",
		"--read-only",
		"--ipc", "none",
		"--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=" + d.tmpfsLimit(),
		"--memory", d.MemoryLimit,
		// Published envelope: no swap. Without this a host with swap lets one
		// validator absorb what another OOM-kills, so the same harness would
		// score on one host and be charged on the next.
		"--memory-swap", d.MemoryLimit,
		"--cpus", d.CPULimit,
		"--pids-limit", strconv.Itoa(d.pidsLimit()),
		"--ulimit", "nofile=1024:1024",
		"--security-opt", "no-new-privileges",
		"--log-driver", "local",
		"--log-opt", "max-size=8m",
		"--log-opt", "max-file=1",
		// Docker 28's containerd-backed local driver defaults compression on,
		// but compression is invalid when the retained-file count is one. Keep
		// the tighter single-file/8 MiB bound and disable unusable rotation
		// compression explicitly so rootless executors fail closed consistently.
		"--log-opt", "compress=false",
	}
	if identity != "" {
		args = append(args,
			"--name", "dittobench-"+identity,
			"--hostname", "harness",
			"--label", "io.heyditto.dittobench.run="+identity,
		)
	}
	if d.Harden {
		// An untrusted userland HTTP harness needs no Linux capabilities.
		args = append(args, "--cap-drop", "ALL")
	}
	if d.SeccompProfile != "" {
		args = append(args, "--security-opt", "seccomp="+d.SeccompProfile)
	}
	if d.AppArmorProfile != "" {
		args = append(args, "--security-opt", "apparmor="+d.AppArmorProfile)
	}
	if network != "" {
		// The egress-restricted sandbox network (allowlisting proxy + host
		// firewall) instead of the default full-egress bridge. See
		// docs/sandbox-egress.md.
		args = append(args, "--network", network)
	}
	// Let the harness reach only the trusted ticket broker at the documented
	// host.docker.internal name. The executor firewall admits the broker port and
	// rejects metadata, sibling, provider, and public-network destinations.
	hostGateway := "host-gateway"
	if d.RequireRootless {
		if discovered, err := d.sandboxHostGateway(); err == nil {
			hostGateway = discovered
		}
	}
	args = append(args,
		"--add-host", "host.docker.internal:"+hostGateway,
		"--publish", "127.0.0.1:0:"+d.HarnessPort, // random host port, loopback only
	)
	capabilityHost, _ := brokerCapabilityHostFromEnv(env)
	if capabilityHost != "" {
		args = append(args, "--add-host", capabilityHost+":"+hostGateway)
	}
	shimEnabled := d.OpenRouterShimCABundleHostPath != ""
	if shimEnabled {
		args = append(args,
			"--add-host", openRouterShimHost+":"+hostGateway,
			"--mount", "type=bind,src="+d.OpenRouterShimCABundleHostPath+
				",dst="+OpenRouterShimCABundlePath+",readonly",
		)
	}
	for k, v := range env {
		if shimEnabled && openRouterShimTLSKey(k) {
			continue
		}
		args = append(args, "-e", k+"="+v)
	}
	if shimEnabled {
		for _, key := range []string{
			"SSL_CERT_FILE",
			"REQUESTS_CA_BUNDLE",
			"CURL_CA_BUNDLE",
			"NODE_EXTRA_CA_CERTS",
		} {
			args = append(args, "-e", key+"="+OpenRouterShimCABundlePath)
		}
	}
	if d.EgressProxy != "" {
		// Force the harness's outbound calls through the allowlisting proxy; the
		// ticket broker and loopback bypass it via NO_PROXY.
		noProxy := "host.docker.internal,localhost,127.0.0.1"
		if capabilityHost != "" {
			noProxy += "," + capabilityHost
		}
		if shimEnabled {
			noProxy += "," + openRouterShimHost
		}
		args = append(args,
			"-e", "HTTPS_PROXY="+d.EgressProxy,
			"-e", "HTTP_PROXY="+d.EgressProxy,
			"-e", "NO_PROXY="+noProxy,
		)
	}
	return append(args, image)
}

func openRouterShimTLSKey(key string) bool {
	switch key {
	case "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS":
		return true
	default:
		return false
	}
}

func brokerCapabilityHostFromEnv(env map[string]string) (string, error) {
	if len(env) == 0 {
		return "", nil
	}
	hosts := make([]string, 0, 2)
	for _, key := range []string{"DITTOBENCH_INFERENCE_BASE_URL", "OLLAMA_BASE_URL"} {
		raw := strings.TrimSpace(env[key])
		if raw == "" {
			continue
		}
		parsed, err := url.Parse(raw)
		if err != nil || parsed.Scheme != "http" || parsed.Hostname() == "" {
			return "", fmt.Errorf("invalid locked broker URL")
		}
		host := strings.ToLower(parsed.Hostname())
		if strings.HasSuffix(host, brokerCapabilityHostSuffix) {
			if !validBrokerCapabilityHost(host) {
				return "", fmt.Errorf("invalid locked broker capability host")
			}
			hosts = append(hosts, host)
		}
	}
	if len(hosts) == 0 {
		return "", nil
	}
	if len(hosts) != 2 || hosts[0] != hosts[1] {
		return "", fmt.Errorf("locked broker capability hosts disagree")
	}
	return hosts[0], nil
}

func validBrokerCapabilityHost(host string) bool {
	label, ok := strings.CutSuffix(host, brokerCapabilityHostSuffix)
	if !ok || len(label) != 54 || !strings.HasPrefix(label, "c-") {
		return false
	}
	for _, char := range label[2:] {
		if (char < 'a' || char > 'z') && (char < '2' || char > '7') {
			return false
		}
	}
	return true
}

func (d *LocalDocker) sandboxHostGateway() (string, error) {
	if value := strings.TrimSpace(d.HostGatewayIP); value != "" {
		parsed := net.ParseIP(value)
		if parsed == nil || parsed.To4() == nil || parsed.IsLoopback() || parsed.IsUnspecified() {
			return "", fmt.Errorf("invalid rootless sandbox host gateway")
		}
		return value, nil
	}
	iface, err := net.InterfaceByName("eth0")
	if err != nil {
		return "", fmt.Errorf("discover rootless sandbox host gateway: eth0 unavailable")
	}
	addrs, err := iface.Addrs()
	if err != nil {
		return "", fmt.Errorf("discover rootless sandbox host gateway: %w", err)
	}
	candidates := make([]string, 0, len(addrs))
	for _, addr := range addrs {
		ip, _, parseErr := net.ParseCIDR(addr.String())
		if parseErr == nil && ip.To4() != nil && !ip.IsLoopback() && !ip.IsUnspecified() {
			candidates = append(candidates, ip.String())
		}
	}
	if len(candidates) == 0 {
		return "", fmt.Errorf("discover rootless sandbox host gateway: eth0 has no IPv4 address")
	}
	sort.Strings(candidates)
	return candidates[0], nil
}

func isolatedIdentity() (string, error) {
	var value [8]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "", fmt.Errorf("sandbox identity: %w", err)
	}
	return fmt.Sprintf("%x", value[:]), nil
}

func (d *LocalDocker) createIsolatedNetwork(ctx context.Context, identity string) (string, error) {
	name := "ditto-job-" + identity
	bridge := "dtj" + identity[:10]
	out, err := d.dockerOutput(
		ctx,
		"network", "create",
		"--driver", "bridge",
		"--opt", "com.docker.network.bridge.name="+bridge,
		"--opt", "com.docker.network.bridge.enable_icc=false",
		"--label", "io.heyditto.dittobench.run="+identity,
		name,
	)
	if err != nil {
		return "", fmt.Errorf("create isolated network: %s: %w", strings.TrimSpace(string(out)), err)
	}
	return name, nil
}

type dockerState struct {
	Running   bool `json:"Running"`
	OOMKilled bool `json:"OOMKilled"`
	ExitCode  int  `json:"ExitCode"`
}

// Diagnostics collects Docker state plus best-effort cgroup-v2 and /tmp usage.
// The inner image may be distroless or already stopped, so unavailable counters
// stay nil while the authoritative Docker State remains available.
func (d *LocalDocker) Diagnostics(ctx context.Context, h *Handle) RuntimeDiagnostics {
	diagnostics := RuntimeDiagnostics{}
	if h == nil || h.ContainerID == "" {
		return diagnostics
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	if out, err := d.dockerOutput(ctx, "inspect", "--format", "{{json .State}}", h.ContainerID); err == nil {
		var state dockerState
		if json.Unmarshal(bytes.TrimSpace(out), &state) == nil {
			diagnostics.StateKnown = true
			diagnostics.Running = state.Running
			diagnostics.OOMKilled = state.OOMKilled
			diagnostics.ExitCode = state.ExitCode
		}
	}
	if h.memoryLimitBytes > 0 {
		limit := h.memoryLimitBytes
		diagnostics.MemoryLimitBytes = &limit
	}
	if diagnostics.Running {
		const script = `
printf '%s\n' __memory_events__
cat /sys/fs/cgroup/memory.events 2>/dev/null || true
printf '%s\n' __memory_peak__
cat /sys/fs/cgroup/memory.peak 2>/dev/null || true
printf '%s\n' __memory_current__
cat /sys/fs/cgroup/memory.current 2>/dev/null || true
printf '%s\n' __tmpfs__
df -Pk /tmp 2>/dev/null | tail -n 1 || true
`
		if out, err := d.dockerOutput(ctx, "exec", h.ContainerID, "/bin/sh", "-c", script); err == nil {
			parseRuntimeMetrics(&diagnostics, string(out))
		}
		// The container is alive, so the host reading is current evidence.
		if h.watch != nil {
			h.watch.sampleHost(d.readHostMemAvailable)
		} else if available, ok := d.readHostMemAvailable(); ok {
			diagnostics.HostMemAvailableMinBytes = &available
		}
	}
	// A container the kernel killed took its cgroup with it; the sampler's
	// last pre-kill snapshot is the evidence that remains.
	h.watch.mergeInto(&diagnostics)
	return diagnostics
}

// ContainerLogTailBytes bounds the container log tail attached to a failed run.
// It mirrors ditto-screener's _LOG_TAIL_BYTES so a screening rejection and a
// benchmark failure hand an operator the same amount of evidence, and so the
// two surfaces stay comparable when the same image fails on both.
const ContainerLogTailBytes = 2000

// containerLogLines pre-bounds what Docker is asked to return. The screener
// reads the whole log and byte-tails it in Python; that is safe there because a
// screening container is short-lived, but a benchmark harness runs for up to 90
// minutes and can emit gigabytes. Asking Docker for the last N lines keeps the
// unbounded case out of this process's memory entirely; ContainerLogTailBytes
// then applies the same final bound the screener does.
const containerLogLines = "500"

// Logs returns a bounded, redacted tail of the container's merged stdout and
// stderr. It must be called before Stop: `docker logs` cannot read a removed
// container, which is exactly why the benchmark path used to report
// "harness exited before health: exit_code=1" and nothing else -- the evidence
// existed, and teardown destroyed it before anything read it.
//
// Returns "" when the container produced no output or the runtime could not be
// queried, so a caller can distinguish "no evidence" from "empty evidence"
// without inventing a placeholder (the screener makes the same distinction by
// leaving its detail untouched when no section had content).
func (d *LocalDocker) Logs(ctx context.Context, h *Handle) string {
	if h == nil || h.ContainerID == "" {
		return ""
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	out, err := d.dockerOutput(ctx, "logs", "--tail", containerLogLines, h.ContainerID)
	text := strings.TrimSpace(string(out))
	if err != nil && text == "" {
		return ""
	}
	if text == "" {
		return ""
	}
	return tail(redactContainerLog(text, h.injectedSecrets), ContainerLogTailBytes)
}

// urlQueryPattern matches the query string of an http(s) URL. A presigned
// artifact URL is the one credential that can plausibly reach a harness's own
// output (it is handed to nothing in the sandbox today, but the log tail is a
// new egress path and it should not be the thing that makes that change).
var urlQueryPattern = regexp.MustCompile(`(https?://[^\s"'<>]*)\?[^\s"'<>]*`)

// redactContainerLog masks values the validator injected before miner output is
// stored or surfaced. The rule is deliberately mechanical: anything this run put
// into the container's environment under a credential-shaped name cannot come
// back out through the container's logs.
//
// Under benchmark v7 this is currently a no-op by construction -- every
// credential-shaped value harnessSandboxEnv injects is the fixed non-secret
// placeholder "ticket", which is below secretValueFloor and is not masked, so no
// useful log text is destroyed. It exists so that if a real credential is ever
// injected, it is redacted on the day it is added rather than on the day someone
// notices it in a miner-visible envelope.
func redactContainerLog(text string, secrets []string) string {
	for _, secret := range secrets {
		text = strings.ReplaceAll(text, secret, "<redacted>")
	}
	return urlQueryPattern.ReplaceAllString(text, "${1}?<redacted>")
}

// secretValueFloor is the shortest injected value worth masking. Short values
// ("ticket", "relay", "1", "true") are placeholders and common English
// substrings; masking them would shred the log without protecting anything.
const secretValueFloor = 8

// credentialEnvValues selects the injected values Logs will mask: those under a
// key naming a credential, long enough to be one.
func credentialEnvValues(env map[string]string) []string {
	var values []string
	for key, value := range env {
		if len(value) < secretValueFloor || !credentialEnvKey(key) {
			continue
		}
		values = append(values, value)
	}
	// Longest first, so masking a short secret cannot leave a fragment of a
	// longer one that contains it.
	sort.Slice(values, func(i, j int) bool { return len(values[i]) > len(values[j]) })
	return values
}

func credentialEnvKey(name string) bool {
	upper := strings.ToUpper(name)
	for _, marker := range [...]string{"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"} {
		if strings.Contains(upper, marker) {
			return true
		}
	}
	return false
}

func parseRuntimeMetrics(diagnostics *RuntimeDiagnostics, output string) {
	section := ""
	for _, raw := range strings.Split(output, "\n") {
		line := strings.TrimSpace(raw)
		switch line {
		case "__memory_events__", "__memory_peak__", "__memory_current__", "__tmpfs__":
			section = line
			continue
		}
		if line == "" {
			continue
		}
		switch section {
		case "__memory_events__":
			fields := strings.Fields(line)
			if len(fields) != 2 {
				continue
			}
			value, err := strconv.ParseUint(fields[1], 10, 64)
			if err != nil {
				continue
			}
			if diagnostics.MemoryEvents == nil {
				diagnostics.MemoryEvents = make(map[string]uint64)
			}
			diagnostics.MemoryEvents[fields[0]] = value
		case "__memory_peak__", "__memory_current__":
			// memory.peak is the high-water mark; memory.current is a floor
			// for it on kernels older than 5.19 that lack memory.peak.
			value, err := strconv.ParseUint(line, 10, 64)
			if err == nil && (diagnostics.MemoryPeakBytes == nil || value > *diagnostics.MemoryPeakBytes) {
				diagnostics.MemoryPeakBytes = &value
			}
		case "__tmpfs__":
			fields := strings.Fields(line)
			if len(fields) < 6 {
				continue
			}
			capacityKB, capErr := strconv.ParseUint(fields[len(fields)-5], 10, 64)
			usedKB, usedErr := strconv.ParseUint(fields[len(fields)-4], 10, 64)
			if capErr == nil && usedErr == nil {
				capacity := capacityKB * 1024
				used := usedKB * 1024
				diagnostics.TmpfsCapacityBytes = &capacity
				diagnostics.TmpfsUsedBytes = &used
			}
		}
	}
}

// Run starts the image detached with resource caps and a random host port, then
// resolves the mapped host port.
func (d *LocalDocker) Run(ctx context.Context, image string, env map[string]string) (*Handle, error) {
	return d.run(ctx, image, env, false)
}

// RunRetainingFailedHandle preserves exact container/network cleanup authority
// after a possibly side-effecting start failure. A nonnil handle MUST be stopped
// with StopRetainingImage even when err is nonnil. It performs no best-effort
// cleanup that could discard an ambiguous container. Legacy Run is unchanged.
func (d *LocalDocker) RunRetainingFailedHandle(ctx context.Context, image string, env map[string]string) (*Handle, error) {
	return d.run(ctx, image, env, true)
}

func (d *LocalDocker) run(ctx context.Context, image string, env map[string]string, retainFailure bool) (*Handle, error) {
	runCtx, cancel := context.WithTimeout(ctx, d.startTimeout())
	defer cancel()
	if _, err := brokerCapabilityHostFromEnv(env); err != nil {
		return nil, err
	}

	identity, err := isolatedIdentity()
	if err != nil {
		return nil, err
	}
	containerName := "dittobench-" + identity
	network := ""
	if d.EgressNetwork != "" {
		network, err = d.createIsolatedNetwork(runCtx, identity)
		if err != nil {
			if retainFailure {
				return &Handle{ContainerID: containerName, ImageRef: image, NetworkName: "ditto-job-" + identity}, err
			}
			return nil, err
		}
	}
	cleanupNetwork := func() {
		if network != "" {
			_, _ = d.dockerOutput(context.Background(), "network", "rm", network)
		}
	}
	args := d.runArgsForNetwork(image, env, network, identity)
	if limit, ok := memoryLimitFromContext(ctx); ok {
		scoped := *d
		scoped.MemoryLimit = limit
		args = scoped.runArgsForNetwork(image, env, network, identity)
	}
	out, err := d.dockerOutput(runCtx, args...)
	if err != nil {
		if retainFailure {
			return &Handle{ContainerID: containerName, ImageRef: image, NetworkName: network}, fmt.Errorf("sandbox start requires cleanup: %w", err)
		}
		// `docker run` can create the named container before a slow image unpack or
		// runtime start exceeds the deadline. Remove that partial object by the
		// exact random identity before releasing its network and image tag.
		_, _ = d.dockerOutput(context.Background(), "rm", "-f", containerName)
		cleanupNetwork()
		d.Release(context.Background(), image)
		return nil, fmt.Errorf("docker run failed: %s: %w", strings.TrimSpace(string(out)), err)
	}
	containerID := strings.TrimSpace(string(out))
	memoryLimit := d.MemoryLimit
	if limit, ok := memoryLimitFromContext(ctx); ok {
		memoryLimit = limit
	}
	limitBytes, _ := parseDockerMemory(memoryLimit)

	hostPort, err := d.mappedPort(runCtx, containerID)
	if err != nil {
		if retainFailure {
			return &Handle{ContainerID: containerName, ImageRef: image, NetworkName: network}, err
		}
		d.Stop(context.Background(), &Handle{ContainerID: containerID, ImageRef: image, NetworkName: network})
		return nil, err
	}
	sourceIP, err := d.containerIP(runCtx, containerID)
	if err != nil {
		if retainFailure {
			return &Handle{ContainerID: containerName, ImageRef: image, NetworkName: network}, err
		}
		d.Stop(context.Background(), &Handle{ContainerID: containerID, ImageRef: image, NetworkName: network})
		return nil, err
	}
	return &Handle{
		ContainerID:      containerID,
		BaseURL:          "http://127.0.0.1:" + hostPort,
		ImageRef:         image,
		NetworkName:      network,
		SourceIP:         sourceIP,
		injectedSecrets:  credentialEnvValues(env),
		memoryLimitBytes: limitBytes,
		watch:            d.startMemoryWatch(containerID, limitBytes),
	}, nil
}

func (d *LocalDocker) containerIP(ctx context.Context, containerID string) (string, error) {
	out, err := d.dockerOutput(ctx, "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", containerID)
	if err != nil {
		return "", fmt.Errorf("docker inspect container address: %w", err)
	}
	value := strings.TrimSpace(string(out))
	if net.ParseIP(value) == nil {
		return "", fmt.Errorf("docker inspect returned invalid container address")
	}
	return value, nil
}

// mappedPort returns the host port docker assigned to the harness port.
func (d *LocalDocker) mappedPort(ctx context.Context, containerID string) (string, error) {
	out, err := exec.CommandContext(ctx, "docker", "port", containerID, d.HarnessPort).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("docker port: %s: %w", strings.TrimSpace(string(out)), err)
	}
	// Output like "127.0.0.1:49160" (possibly multiple lines for v4/v6).
	first := strings.SplitN(strings.TrimSpace(string(out)), "\n", 2)[0]
	idx := strings.LastIndex(first, ":")
	if idx < 0 {
		return "", fmt.Errorf("unexpected docker port output: %q", first)
	}
	return strings.TrimSpace(first[idx+1:]), nil
}

// Stop force-removes the container and its request-scoped image tag. Errors are
// ignored (best-effort cleanup); removing the container first lets Docker reclaim
// the image layers when no concurrent run still references them.
func (d *LocalDocker) Stop(ctx context.Context, h *Handle) {
	if h == nil {
		return
	}
	_ = d.StopRetainingImage(ctx, h)
	d.Release(ctx, h.ImageRef)
}

func (d *LocalDocker) StopRetainingImage(ctx context.Context, h *Handle) error {
	if h == nil || strings.TrimSpace(h.ContainerID) == "" {
		return fmt.Errorf("sandbox container identity is unavailable")
	}
	h.watch.stop()
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	removeOutput, removeErr := exec.CommandContext(ctx, "docker", "rm", "-f", h.ContainerID).CombinedOutput()
	inspectOutput, inspectErr := exec.CommandContext(ctx, "docker", "inspect", "--format", "{{.Id}}", h.ContainerID).CombinedOutput()
	if inspectErr == nil {
		return fmt.Errorf("sandbox container removal was not confirmed")
	}
	missing := strings.Contains(string(inspectOutput), "No such object") ||
		strings.Contains(string(inspectOutput), "No such container")
	if !missing {
		return fmt.Errorf("verify sandbox container removal: %w", inspectErr)
	}
	if removeErr != nil && !strings.Contains(string(removeOutput), "No such container") {
		return fmt.Errorf("remove sandbox container: %w", removeErr)
	}
	if h.NetworkName != "" {
		if !strings.HasPrefix(h.NetworkName, "ditto-job-") {
			return fmt.Errorf("sandbox network identity is invalid")
		}
		networkRemoveOutput, networkRemoveErr := d.dockerOutput(ctx, "network", "rm", h.NetworkName)
		networkInspectOutput, networkInspectErr := d.dockerOutput(ctx, "network", "inspect", h.NetworkName)
		if networkInspectErr == nil {
			return fmt.Errorf("sandbox network removal was not confirmed")
		}
		networkMissing := strings.Contains(string(networkInspectOutput), "No such network") ||
			strings.Contains(string(networkInspectOutput), "not found")
		if !networkMissing {
			return fmt.Errorf("verify sandbox network removal: %w", networkInspectErr)
		}
		if networkRemoveErr != nil && !strings.Contains(string(networkRemoveOutput), "No such network") {
			return fmt.Errorf("remove sandbox network: %w", networkRemoveErr)
		}
	}
	return nil
}

func (d *LocalDocker) Release(ctx context.Context, image string) {
	// Only delete images in the namespace Build owns. Run remains safe to use
	// with an operator-managed image without unexpectedly deleting it.
	if !strings.HasPrefix(image, "dittobench-sub:") {
		return
	}
	cleanupCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	_ = exec.CommandContext(cleanupCtx, "docker", "image", "rm", image).Run()
}

// safeTag derives a docker-safe tag from the source ref.
func safeTag(src Source) string {
	base := src.GitURL
	if base == "" {
		// Tarball submissions: tag off the URL path (minus query/signature).
		base = src.TarballURL
		if i := strings.IndexByte(base, '?'); i >= 0 {
			base = base[:i]
		}
	}
	if i := strings.LastIndex(base, "/"); i >= 0 {
		base = base[i+1:]
	}
	base = strings.TrimSuffix(base, ".git")
	base = strings.TrimSuffix(base, ".tar.gz")
	base = strings.TrimSuffix(base, ".tgz")
	var suffix string
	switch {
	case src.GitRef != "":
		suffix = "-" + src.GitRef
	case src.TarballSHA256 != "":
		// Pin the tag to the content hash so re-runs of the same blob reuse the
		// build cache and distinct blobs never collide on a tag.
		if len(src.TarballSHA256) >= 12 {
			suffix = "-" + src.TarballSHA256[:12]
		} else {
			suffix = "-" + src.TarballSHA256
		}
	}
	// A screened image is already content-addressed. Include its image ID even
	// when the practice caller omitted tarball_sha256, preventing two archives
	// with the same URL basename from sharing a mutable local tag.
	if id := strings.TrimPrefix(src.ScreenedImageID, "sha256:"); id != "" {
		if len(id) > 12 {
			id = id[:12]
		}
		suffix += "-image-" + id
	}
	base += suffix
	var b strings.Builder
	for _, r := range strings.ToLower(base) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-', r == '.', r == '_':
			b.WriteRune(r)
		default:
			b.WriteByte('-')
		}
	}
	t := strings.Trim(b.String(), "-._")
	if t == "" {
		t = "submission"
	}
	if len(t) > 100 {
		// Preserve the content-addressed suffix when long presigned object names
		// would otherwise truncate it away.
		cleanSuffix := strings.TrimLeft(safeTagPart(suffix), "-._")
		if cleanSuffix != "" && len(cleanSuffix) < 99 {
			t = strings.TrimRight(t[:99-len(cleanSuffix)], "-._") + "-" + cleanSuffix
		} else {
			t = t[:100]
		}
	}
	return t
}

func safeTagPart(value string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(value) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-', r == '.', r == '_':
			b.WriteRune(r)
		default:
			b.WriteByte('-')
		}
	}
	return b.String()
}

// tail returns the last n bytes of s, prefixed with an ellipsis if truncated.
func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n:]
}
