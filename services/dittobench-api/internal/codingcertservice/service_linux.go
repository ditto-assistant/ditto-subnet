package codingcertservice

import (
	"context"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcanary"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinghost"
	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"golang.org/x/sys/unix"
)

const (
	lockedPolicySHA256 = "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
	maxTokenBytes      = 256
	startupTimeout     = 30 * time.Second
	shutdownTimeout    = 30 * time.Second
)

var (
	// ErrPlacement refuses startup when the rootless topology is not proven.
	ErrPlacement = errors.New("coding certification service placement is not proven")
	// ErrUnavailable is any other startup refusal. It carries no detail.
	ErrUnavailable = errors.New("coding certification service is unavailable")
)

// Routes exposes exactly the canary and its readiness probe. Every other path
// is not found; the handlers enforce their own method, bearer and body rules.
func Routes(canary, readiness http.Handler) http.Handler {
	mux := http.NewServeMux()
	mux.Handle(codingcanary.CanaryPath, canary)
	mux.Handle(codingcanary.ReadinessPath, readiness)
	return mux
}

// Run starts the service and blocks until ctx ends. Nothing is created before
// the configuration, credential, policy, pinned pack and rootless topology are
// all proven: no private state, router listener or control socket.
func Run(ctx context.Context, getenv func(string) string, euid int) error {
	config, err := ConfigFromEnvironment(getenv, euid)
	if err != nil {
		return err
	}
	token, err := readControlToken(config.CredentialDirectory)
	if err != nil {
		return ErrUnavailable
	}
	policy, err := loadPolicy(config.PolicyPath)
	if err != nil {
		return ErrUnavailable
	}
	pack, err := codingcanary.LoadPublicPack(config.CertificationRoot)
	if err != nil || pack.CanaryManifestSHA256 != config.PackManifestSHA256 {
		return ErrUnavailable
	}
	docker, err := sandbox.NewCodingRuntimeDocker(config.Runtime)
	if err != nil {
		return ErrConfig
	}
	// The router listener runs the pinned helper through nsenter; both must be
	// root-owned and unwritable by anyone else before either is executed.
	if VerifyTrustedExecutable(config.RouterHelperPath, config.RouterHelperSHA256) != nil ||
		VerifyTrustedExecutable(rootlessnetns.NsenterExecutable, "") != nil {
		return ErrPlacement
	}
	placement := newPlacement(config, euid, nil, nil)
	startup, cancel := context.WithTimeout(ctx, startupTimeout)
	defer cancel()
	if !placement.RootlessTopology(startup) {
		return ErrPlacement
	}
	if err := os.Mkdir(config.PrivateRoot, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return ErrUnavailable
	}
	router, err := rootlessnetns.Listen(startup, config.routerConfig())
	if err != nil {
		return ErrPlacement
	}
	placement.router = router
	// codinghost owns and closes the router listener from here, including on
	// construction failure.
	host, err := codinghost.New(codinghost.Config{
		ControlToken: token, PrivateRoot: config.PrivateRoot, SourceListener: router,
		SourcePublicBaseURL:    "http://host.docker.internal:" + strconv.Itoa(int(config.RouterListen.Port())),
		Policy:                 policy,
		RuntimeImageRepository: config.RuntimeRepository, RuntimeImageDigest: config.RuntimeImageDigest,
		CanaryEnabled: true, CertificationRoot: config.CertificationRoot, CanaryTopology: placement.Check,
		Docker: docker, CandidateUID: CandidateUID, CandidateGID: CandidateGID,
		MaxTotalBytes: 16 << 30, JournalMaxTotalBytes: 3 << 30, MaxAttempts: 64,
	})
	if err != nil {
		return ErrUnavailable
	}
	closeHost := func() {
		closing, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		_ = host.Close(closing)
	}
	if placement.verifyListener(startup, router, config.routerConfig()) != nil {
		closeHost()
		return ErrPlacement
	}
	control, err := ListenControlSocket(config.ControlSocketPath, euid, config.ControlGID)
	if err != nil {
		closeHost()
		return ErrUnavailable
	}
	placement.control = control
	server := &http.Server{
		Handler:           Routes(host.CanaryHandler(), host.CanaryReadinessHandler()),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
		ErrorLog:          log.New(io.Discard, "", 0),
	}
	served := make(chan error, 1)
	go func() { served <- server.Serve(control.Listener()) }()
	var serveErr error
	select {
	case <-ctx.Done():
	case serveErr = <-served:
	}
	shutdown, cancelShutdown := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancelShutdown()
	_ = server.Shutdown(shutdown)
	_ = control.Close()
	closeHost()
	if serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
		return ErrUnavailable
	}
	return nil
}

// readControlToken reads the systemd credential without following links.
func readControlToken(directory string) (string, error) {
	if !filepath.IsAbs(directory) || filepath.Clean(directory) != directory {
		return "", ErrUnavailable
	}
	fd, err := unix.Open(filepath.Join(directory, ControlTokenCredential), unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return "", ErrUnavailable
	}
	file := os.NewFile(uintptr(fd), "control-token")
	defer file.Close()
	var stat unix.Stat_t
	if unix.Fstat(fd, &stat) != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Size > maxTokenBytes+1 {
		return "", ErrUnavailable
	}
	body, err := io.ReadAll(io.LimitReader(file, maxTokenBytes+2))
	if err != nil {
		return "", ErrUnavailable
	}
	token := string(body)
	if n := len(token); n > 0 && token[n-1] == '\n' {
		token = token[:n-1]
	}
	if !validControlToken(token) {
		return "", ErrUnavailable
	}
	return token, nil
}

func validControlToken(value string) bool {
	if len(value) < 32 || len(value) > maxTokenBytes {
		return false
	}
	for _, character := range value {
		if !(character >= 'a' && character <= 'z' || character >= 'A' && character <= 'Z' ||
			character >= '0' && character <= '9' || character == '-' || character == '_') {
			return false
		}
	}
	return true
}

func loadPolicy(path string) (codingcontract.InferencePolicy, error) {
	var zero codingcontract.InferencePolicy
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return zero, ErrUnavailable
	}
	file := os.NewFile(uintptr(fd), "coding-policy")
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, codingcontract.MaxInferencePolicyBytes+1))
	if err != nil || len(body) == 0 || len(body) > codingcontract.MaxInferencePolicyBytes ||
		codingcontract.ValidateJSONDocument(body, codingcontract.MaxInferencePolicyBytes) != nil {
		return zero, ErrUnavailable
	}
	policy, err := codingcontract.ParseInferencePolicy(body)
	if err != nil {
		return zero, ErrUnavailable
	}
	digest, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil || digest != lockedPolicySHA256 {
		return zero, ErrUnavailable
	}
	return policy, nil
}
