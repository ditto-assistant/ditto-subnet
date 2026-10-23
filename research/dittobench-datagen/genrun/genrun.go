// Package genrun builds and runs DittoBench generator programs.
//
// A Sandbox turns a generator source directory into a runnable program and
// runs it on one GenerateRequest. Docker is the scorer's sandbox: the build
// and every run happen in pinned images with no network, a read-only root
// filesystem, dropped capabilities, and CPU, memory, process, and wall-time
// limits. Local builds and runs on the host with the miner's own toolchain; it
// isolates nothing and exists for fast rehearsal.
//
// Evaluate is the whole generator-side half of a scorer run: build once, run
// every seed, validate each pack against its request, and re-run the first
// seed to check determinism. It separates the generator's failures (compile
// errors, crashes, timeouts, invalid or nondeterministic output), which are
// recorded in the Evaluation, from sandbox failures, which are returned as
// errors so the scorer never blames a generator for its own infrastructure.
package genrun

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

// Pinned scorer images. The builder's Go version is challenge.BuilderGoVersion.
const (
	BuilderImage = "golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36"
	RunnerImage  = "gcr.io/distroless/static:nonroot@sha256:e2e927ec666bae08560abb3c55d0659eceabb657f56b6782ab500a9fc7f555e3"
)

// Limits bound one build and one generation run.
type Limits struct {
	BuildTimeout    time.Duration
	GenerateTimeout time.Duration
	MemoryBytes     int64
	CPUs            string
	PIDs            int
}

// DefaultLimits are the v1 scorer limits.
func DefaultLimits() Limits {
	return Limits{
		BuildTimeout:    5 * time.Minute,
		GenerateTimeout: challenge.GenerateTimeoutSeconds * time.Second,
		MemoryBytes:     challenge.GenerateMemoryBytes,
		CPUs:            "1",
		PIDs:            64,
	}
}

// ErrSandbox wraps a failure of the sandbox itself (docker unavailable, image
// missing, host I/O). It is never the generator's fault.
var ErrSandbox = errors.New("generator sandbox failure")

// GeneratorError is a failure the generator caused.
type GeneratorError struct {
	Phase  string // "build" or "generate"
	Reason string // "compile_failed", "timeout", "exit", "output_too_large"
	Detail string // bounded stderr tail
}

func (e *GeneratorError) Error() string {
	if e.Detail == "" {
		return fmt.Sprintf("generator %s: %s", e.Phase, e.Reason)
	}
	return fmt.Sprintf("generator %s: %s: %s", e.Phase, e.Reason, e.Detail)
}

// Program is a built generator.
type Program interface {
	// Generate runs the program on one request and returns its stdout.
	Generate(ctx context.Context, req challenge.GenerateRequest) ([]byte, error)
	// SHA256 identifies the built binary.
	SHA256() string
	// Close releases the build.
	Close() error
}

// Sandbox builds generator programs.
type Sandbox interface {
	Build(ctx context.Context, srcDir string) (Program, error)
	Name() string
}

const stderrTail = 4 << 10

type tailBuffer struct{ buf []byte }

func (t *tailBuffer) Write(p []byte) (int, error) {
	t.buf = append(t.buf, p...)
	if len(t.buf) > stderrTail {
		t.buf = append([]byte(nil), t.buf[len(t.buf)-stderrTail:]...)
	}
	return len(p), nil
}

func (t *tailBuffer) String() string { return strings.TrimSpace(string(t.buf)) }

// boundedBuffer keeps at most limit bytes and records overflow.
type boundedBuffer struct {
	buf      bytes.Buffer
	limit    int
	overflow bool
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	if room := b.limit - b.buf.Len(); room < len(p) {
		if room > 0 {
			b.buf.Write(p[:room])
		}
		b.overflow = true
		return len(p), nil
	}
	return b.buf.Write(p)
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func vendored(srcDir string) bool {
	_, err := os.Stat(filepath.Join(srcDir, "vendor", "modules.txt"))
	return err == nil
}

// --- Local -------------------------------------------------------------

// Local builds with the host toolchain and runs on the host. No isolation.
type Local struct{ Limits Limits }

func (Local) Name() string { return "local" }

func (l Local) Build(ctx context.Context, srcDir string) (Program, error) {
	limits := l.Limits
	if limits.BuildTimeout == 0 {
		limits = DefaultLimits()
	}
	out, err := os.MkdirTemp("", "genrun-local-*")
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrSandbox, err)
	}
	bin := filepath.Join(out, "generator")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	bctx, cancel := context.WithTimeout(ctx, limits.BuildTimeout)
	defer cancel()
	cmd := exec.CommandContext(bctx, "go", "build", "-trimpath", "-buildvcs=false", "-o", bin, ".")
	cmd.Dir = srcDir
	env := append(os.Environ(), "CGO_ENABLED=0", "GOPROXY=off", "GOFLAGS=")
	if vendored(srcDir) {
		env = append(env, "GOFLAGS=-mod=vendor")
	}
	cmd.Env = env
	var stderr tailBuffer
	cmd.Stdout, cmd.Stderr = &stderr, &stderr
	if err := cmd.Run(); err != nil {
		os.RemoveAll(out)
		var exit *exec.ExitError
		if bctx.Err() != nil {
			return nil, &GeneratorError{Phase: "build", Reason: "timeout"}
		}
		if errors.As(err, &exit) {
			return nil, &GeneratorError{Phase: "build", Reason: "compile_failed", Detail: stderr.String()}
		}
		return nil, fmt.Errorf("%w: go build: %v", ErrSandbox, err)
	}
	sum, err := fileSHA256(bin)
	if err != nil {
		os.RemoveAll(out)
		return nil, fmt.Errorf("%w: %v", ErrSandbox, err)
	}
	return &localProgram{dir: out, bin: bin, sum: sum, limits: limits}, nil
}

type localProgram struct {
	dir, bin, sum string
	limits        Limits
}

func (p *localProgram) SHA256() string { return p.sum }
func (p *localProgram) Close() error   { return os.RemoveAll(p.dir) }

func (p *localProgram) Generate(ctx context.Context, req challenge.GenerateRequest) ([]byte, error) {
	gctx, cancel := context.WithTimeout(ctx, p.limits.GenerateTimeout)
	defer cancel()
	cmd := exec.CommandContext(gctx, p.bin)
	cmd.Dir = p.dir
	cmd.Env = []string{"HOME=" + p.dir, "TMPDIR=" + p.dir}
	return runGenerate(gctx, cmd, req, nil)
}

func runGenerate(ctx context.Context, cmd *exec.Cmd, req challenge.GenerateRequest, isSandboxExit func(int) bool) ([]byte, error) {
	cmd.Stdin = bytes.NewReader(req.Bytes())
	stdout := &boundedBuffer{limit: challenge.MaxPackBytes}
	var stderr tailBuffer
	cmd.Stdout, cmd.Stderr = stdout, &stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		return nil, &GeneratorError{Phase: "generate", Reason: "timeout"}
	}
	if err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) {
			return nil, fmt.Errorf("%w: %v", ErrSandbox, err)
		}
		if isSandboxExit != nil && isSandboxExit(exit.ExitCode()) {
			return nil, fmt.Errorf("%w: sandbox exited %d: %s", ErrSandbox, exit.ExitCode(), stderr.String())
		}
		reason := "exit"
		if exit.ExitCode() == 137 {
			reason = "killed"
		}
		return nil, &GeneratorError{Phase: "generate", Reason: reason, Detail: fmt.Sprintf("exit %d: %s", exit.ExitCode(), stderr.String())}
	}
	if stdout.overflow {
		return nil, &GeneratorError{Phase: "generate", Reason: "output_too_large", Detail: fmt.Sprintf("stdout exceeded %d bytes", challenge.MaxPackBytes)}
	}
	return stdout.buf.Bytes(), nil
}

// --- Docker ------------------------------------------------------------

// Docker builds and runs generators in pinned, network-less containers.
type Docker struct {
	Limits Limits
	// Binary is the docker CLI (default "docker").
	Binary string
	// BuilderImage and RunnerImage override the pinned images (tests only).
	BuilderImage, RunnerImage string
}

func (Docker) Name() string { return "docker" }

func (d Docker) cli() string {
	if d.Binary != "" {
		return d.Binary
	}
	return "docker"
}

func (d Docker) limits() Limits {
	if d.Limits.BuildTimeout == 0 {
		return DefaultLimits()
	}
	return d.Limits
}

func (d Docker) images() (string, string) {
	b, r := d.BuilderImage, d.RunnerImage
	if b == "" {
		b = BuilderImage
	}
	if r == "" {
		r = RunnerImage
	}
	return b, r
}

// Docker CLI exit codes that mean the container never ran the command.
func dockerExit(code int) bool { return code == 125 || code == 126 || code == 127 }

func containerName() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return "genrun-" + hex.EncodeToString(b)
}

// hardening applies to both build and run containers.
func (d Docker) hardening(limits Limits) []string {
	return []string{
		"--rm", "--network=none", "--read-only",
		"--cap-drop=ALL", "--security-opt=no-new-privileges",
		"--user=65532:65532",
		fmt.Sprintf("--memory=%d", limits.MemoryBytes), fmt.Sprintf("--memory-swap=%d", limits.MemoryBytes),
		"--cpus=" + limits.CPUs, fmt.Sprintf("--pids-limit=%d", limits.PIDs),
	}
}

func (d Docker) kill(name string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_ = exec.CommandContext(ctx, d.cli(), "kill", name).Run()
}

func (d Docker) Build(ctx context.Context, srcDir string) (Program, error) {
	limits := d.limits()
	builder, runner := d.images()
	src, err := filepath.Abs(srcDir)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrSandbox, err)
	}
	out, err := os.MkdirTemp("", "genrun-docker-*")
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrSandbox, err)
	}
	// The build user is unprivileged inside the container.
	if err := os.Chmod(out, 0o777); err != nil {
		os.RemoveAll(out)
		return nil, fmt.Errorf("%w: %v", ErrSandbox, err)
	}
	goflags := ""
	if vendored(src) {
		goflags = "-mod=vendor"
	}
	name := containerName()
	buildLimits := limits
	buildLimits.MemoryBytes = 2 << 30
	buildLimits.CPUs = "2"
	buildLimits.PIDs = 512
	args := append([]string{"run", "--name", name}, d.hardening(buildLimits)...)
	args = append(args,
		"--tmpfs=/tmp:rw,exec,nosuid,size=1g",
		"-v", src+":/src:ro", "-v", out+":/out",
		"-w", "/src",
		"-e", "CGO_ENABLED=0", "-e", "GOPROXY=off", "-e", "GOTOOLCHAIN=local",
		"-e", "GOFLAGS="+goflags, "-e", "GOCACHE=/tmp/gocache", "-e", "GOPATH=/tmp/gopath",
		"-e", "HOME=/tmp",
		builder,
		"go", "build", "-trimpath", "-buildvcs=false", "-o", "/out/generator", ".",
	)
	bctx, cancel := context.WithTimeout(ctx, limits.BuildTimeout)
	defer cancel()
	cmd := exec.Command(d.cli(), args...)
	var stderr tailBuffer
	cmd.Stdout, cmd.Stderr = &stderr, &stderr
	if err := startAndWait(bctx, cmd, func() { d.kill(name) }); err != nil {
		os.RemoveAll(out)
		if bctx.Err() != nil && ctx.Err() == nil {
			return nil, &GeneratorError{Phase: "build", Reason: "timeout"}
		}
		var exit *exec.ExitError
		if errors.As(err, &exit) && !dockerExit(exit.ExitCode()) {
			return nil, &GeneratorError{Phase: "build", Reason: "compile_failed", Detail: stderr.String()}
		}
		return nil, fmt.Errorf("%w: docker build container: %v: %s", ErrSandbox, err, stderr.String())
	}
	bin := filepath.Join(out, "generator")
	sum, err := fileSHA256(bin)
	if err != nil {
		os.RemoveAll(out)
		return nil, fmt.Errorf("%w: built binary missing: %v", ErrSandbox, err)
	}
	return &dockerProgram{d: d, dir: out, sum: sum, limits: limits, image: runner}, nil
}

func startAndWait(ctx context.Context, cmd *exec.Cmd, onCancel func()) error {
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		onCancel()
		<-done
		return ctx.Err()
	}
}

type dockerProgram struct {
	d      Docker
	dir    string
	sum    string
	limits Limits
	image  string
}

func (p *dockerProgram) SHA256() string { return p.sum }
func (p *dockerProgram) Close() error   { return os.RemoveAll(p.dir) }

func (p *dockerProgram) Generate(ctx context.Context, req challenge.GenerateRequest) ([]byte, error) {
	name := containerName()
	args := append([]string{"run", "--name", name, "-i"}, p.d.hardening(p.limits)...)
	args = append(args,
		"--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
		"-v", p.dir+":/gen:ro",
		"--entrypoint", "/gen/generator",
		p.image,
	)
	gctx, cancel := context.WithTimeout(ctx, p.limits.GenerateTimeout)
	defer cancel()
	cmd := exec.Command(p.d.cli(), args...)
	cmd.Stdin = bytes.NewReader(req.Bytes())
	stdout := &boundedBuffer{limit: challenge.MaxPackBytes}
	var stderr tailBuffer
	cmd.Stdout, cmd.Stderr = stdout, &stderr
	err := startAndWait(gctx, cmd, func() { p.d.kill(name) })
	if gctx.Err() != nil {
		if ctx.Err() != nil {
			return nil, fmt.Errorf("%w: %v", ErrSandbox, ctx.Err())
		}
		return nil, &GeneratorError{Phase: "generate", Reason: "timeout"}
	}
	if err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) || dockerExit(exit.ExitCode()) {
			return nil, fmt.Errorf("%w: docker run: %v: %s", ErrSandbox, err, stderr.String())
		}
		reason := "exit"
		if exit.ExitCode() == 137 {
			reason = "killed"
		}
		return nil, &GeneratorError{Phase: "generate", Reason: reason, Detail: fmt.Sprintf("exit %d: %s", exit.ExitCode(), stderr.String())}
	}
	if stdout.overflow {
		return nil, &GeneratorError{Phase: "generate", Reason: "output_too_large", Detail: fmt.Sprintf("stdout exceeded %d bytes", challenge.MaxPackBytes)}
	}
	return stdout.buf.Bytes(), nil
}
