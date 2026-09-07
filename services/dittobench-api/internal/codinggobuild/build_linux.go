package codinggobuild

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"go/importer"
	"go/token"
	"go/types"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

const goBinary = "/usr/local/go/bin/go"
const runnerDirectory = "dittobench_internal_runner"
const adapterFile = "zz_dittobench_bridge.go"

// File contains candidate-authorized frozen workspace bytes, not grader source.
type File struct {
	Path  string
	Bytes []byte
}
type Request struct {
	ModulePath  string
	PackageName string
	Files       []File
	Functions   []string
	UID         uint32
	GID         uint32
}
type Artifact struct {
	Directory  string
	Program    string
	SHA256     string
	Exports    map[string]string
	executable *os.File
}

func (a *Artifact) Close() error {
	if a == nil || a.executable == nil {
		return nil
	}
	err := a.executable.Close()
	a.executable = nil
	return err
}

// OpenExecutable returns a verified duplicate of the sealed anonymous file for
// a trusted launcher. Neither this descriptor nor the artifact is a wire DTO.
func (a Artifact) OpenExecutable() (*os.File, error) { return sealedProgram(a) }

// Importer reads only export archives from this completed, private build. It
// never runs a command or performs a network lookup from the oracle process.
func (a Artifact) Importer(fset *token.FileSet) types.Importer {
	return importer.ForCompiler(fset, "gc", func(name string) (io.ReadCloser, error) {
		file, ok := a.Exports[name]
		if !ok {
			return nil, ErrBuild
		}
		fd, err := os.OpenFile(file, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if err != nil {
			return nil, ErrBuild
		}
		info, err := fd.Stat()
		if err != nil || !info.Mode().IsRegular() || info.Size() > 64<<20 {
			fd.Close()
			return nil, ErrBuild
		}
		return fd, nil
	})
}

func (File) String() string                   { return "<private candidate file>" }
func (File) GoString() string                 { return "<private candidate file>" }
func (File) MarshalJSON() ([]byte, error)     { return nil, ErrBuild }
func (Request) String() string                { return "<private Go build request>" }
func (Request) GoString() string              { return "<private Go build request>" }
func (Request) MarshalJSON() ([]byte, error)  { return nil, ErrBuild }
func (Artifact) String() string               { return "<private Go build artifact>" }
func (Artifact) GoString() string             { return "<private Go build artifact>" }
func (Artifact) MarshalJSON() ([]byte, error) { return nil, ErrBuild }

func Environment(root string) []string {
	return []string{
		"PATH=/usr/local/go/bin:/usr/bin:/bin", "GOROOT=/usr/local/go", "GOOS=linux", "GOARCH=amd64", "GOAMD64=v1",
		"CGO_ENABLED=0", "GOTOOLCHAIN=local", "GOENV=off", "GOWORK=off", "GOFLAGS=", "GOPROXY=off", "GOSUMDB=off", "GOVCS=*:off",
		"GOPATH=" + filepath.Join(root, "cache", "gopath"), "GOMODCACHE=" + filepath.Join(root, "cache", "modules"),
		"GOCACHE=" + filepath.Join(root, "cache", "build"), "TMPDIR=" + filepath.Join(root, "cache", "tmp"),
	}
}

func validate(request Request) error {
	if request.UID == 0 || request.GID == 0 || request.UID == ^uint32(0) || request.GID == ^uint32(0) || request.PackageName == "main" || len(request.Files) == 0 || len(request.Files) > 2048 {
		return ErrBuild
	}
	seen := map[string]bool{}
	total := 0
	moduleFound := false
	for _, file := range request.Files {
		name := file.Path
		if name == "" || len(name) > 240 || path.IsAbs(name) || path.Clean(name) != name || name == ".." || strings.HasPrefix(name, "../") || strings.ContainsAny(name, "\\\x00\n\r") || strings.HasSuffix(name, "_test.go") || seen[name] || name == adapterFile || name == runnerDirectory || strings.HasPrefix(name, runnerDirectory+"/") {
			return ErrBuild
		}
		seen[name] = true
		total += len(file.Bytes)
		if total > 32<<20 {
			return ErrBuild
		}
		if name == "go.mod" {
			for _, line := range strings.Split(string(file.Bytes), "\n") {
				fields := strings.Fields(line)
				if len(fields) == 2 && fields[0] == "module" && fields[1] == request.ModulePath {
					moduleFound = true
				}
			}
		}
	}
	if !moduleFound {
		return ErrBuild
	}
	return nil
}

type boundedOutput struct {
	bytes.Buffer
	maximum int
}

func (w *boundedOutput) Write(value []byte) (int, error) {
	if w.Len()+len(value) > w.maximum {
		return 0, ErrBuild
	}
	return w.Buffer.Write(value)
}

func run(ctx context.Context, root string, uid, gid uint32, arguments ...string) ([]byte, error) {
	command := exec.CommandContext(ctx, goBinary, arguments...)
	command.Dir = filepath.Join(root, "project")
	command.Env = Environment(root)
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Credential: &syscall.Credential{Uid: uid, Gid: gid, Groups: []uint32{}}}
	var output boundedOutput
	output.maximum = 16 << 20
	command.Stdout = &output
	command.Stderr = io.Discard
	command.WaitDelay = 5 * time.Second
	command.Cancel = func() error {
		if command.Process == nil {
			return nil
		}
		err := syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
		if errors.Is(err, syscall.ESRCH) {
			return nil
		}
		return err
	}
	if err := command.Start(); err != nil {
		return nil, ErrBuild
	}
	pid := command.Process.Pid
	waitErr := command.Wait()
	if err := reapGroup(pid); err != nil {
		return nil, err
	}
	if waitErr != nil {
		var exit *exec.ExitError
		if ctx.Err() == nil && errors.As(waitErr, &exit) && exit.ExitCode() > 0 {
			return nil, ErrCompilation
		}
		return nil, ErrBuild
	}
	return output.Bytes(), nil
}

func reapGroup(pid int) error {
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		return ErrBuild
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		var status syscall.WaitStatus
		for {
			reaped, err := syscall.Wait4(-pid, &status, syscall.WNOHANG, nil)
			if err != nil || reaped <= 0 {
				break
			}
		}
		if errors.Is(syscall.Kill(-pid, 0), syscall.ESRCH) {
			return nil
		}
		if time.Now().After(deadline) {
			return ErrBuild
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func rootParent() error {
	if os.Geteuid() != 0 {
		return ErrBuild
	}
	body, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return ErrBuild
	}
	fields := map[string]string{}
	for _, line := range strings.Split(string(body), "\n") {
		parts := strings.Fields(line)
		if len(parts) == 2 {
			fields[strings.TrimSuffix(parts[0], ":")] = parts[1]
		}
	}
	// The established trusted executor capability set, with no ambient or
	// inheritable capabilities and no privilege-gain path for compiler children.
	if fields["CapEff"] != "00000000000000e3" || fields["CapPrm"] != "00000000000000e3" || fields["CapInh"] != "0000000000000000" || fields["CapAmb"] != "0000000000000000" || fields["NoNewPrivs"] != "1" {
		return ErrBuild
	}
	if unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != nil {
		return ErrBuild
	}
	return nil
}

// Build runs only fixed, immutable Go toolchain binaries as the candidate UID.
// No go test, go generate, cgo, VCS hook, external linker or module network fetch
// is available. Compiler process groups are killed/reaped before outputs are
// sealed. Failure retains the private directory for explicit operator review.
func Build(ctx context.Context, request Request) (artifact Artifact, err error) {
	if err = validate(request); err != nil {
		return artifact, err
	}
	bridge, err := BridgeSource(request.PackageName, request.Functions)
	if err != nil {
		return artifact, err
	}
	main, err := MainSource(request.ModulePath)
	if err != nil {
		return artifact, err
	}
	if err = rootParent(); err != nil {
		return artifact, err
	}
	info, err := os.Lstat(goBinary)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0022 != 0 {
		return artifact, ErrBuild
	}
	if sys, ok := info.Sys().(*syscall.Stat_t); !ok || sys.Uid != 0 {
		return artifact, ErrBuild
	}
	root, err := os.MkdirTemp("/tmp", "dittobench-go-build-")
	if err != nil {
		return artifact, ErrBuild
	}
	artifact.Directory = root
	defer func() {
		if os.Chmod(root, 0700) != nil {
			err = ErrBuild
		}
		if err != nil && artifact.executable != nil {
			artifact.executable.Close()
			artifact.executable = nil
		}
	}()
	if os.Chmod(root, 0755) != nil {
		return artifact, ErrBuild
	}
	project := filepath.Join(root, "project")
	if os.Mkdir(project, 0755) != nil {
		return artifact, ErrBuild
	}
	files := append([]File(nil), request.Files...)
	files = append(files, File{Path: adapterFile, Bytes: bridge}, File{Path: runnerDirectory + "/main.go", Bytes: main})
	for _, file := range files {
		target := filepath.Join(project, filepath.FromSlash(file.Path))
		if os.MkdirAll(filepath.Dir(target), 0755) != nil {
			return artifact, ErrBuild
		}
		fd, openErr := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0444)
		if openErr != nil {
			return artifact, ErrBuild
		}
		_, writeErr := fd.Write(file.Bytes)
		syncErr := fd.Sync()
		closeErr := fd.Close()
		if writeErr != nil || syncErr != nil || closeErr != nil {
			return artifact, ErrBuild
		}
	}
	for _, relative := range []string{"output", "cache", "cache/build", "cache/modules", "cache/gopath", "cache/tmp"} {
		directory := filepath.Join(root, relative)
		if os.MkdirAll(directory, 0700) != nil || os.Chown(directory, int(request.UID), int(request.GID)) != nil {
			return artifact, ErrBuild
		}
	}
	program := filepath.Join(root, "output", "candidate")
	if _, err = run(ctx, root, request.UID, request.GID, "build", "-mod=readonly", "-buildvcs=false", "-trimpath", "-ldflags=-linkmode=internal", "-o", program, "./"+runnerDirectory); err != nil {
		return artifact, err
	}
	metadata, err := run(ctx, root, request.UID, request.GID, "list", "-mod=readonly", "-buildvcs=false", "-ldflags=-linkmode=internal", "-export", "-deps", "-json", "./"+runnerDirectory, "testing", "reflect", "bytes", "time")
	if err != nil {
		return artifact, err
	}
	artifact.Exports = map[string]string{}
	decoder := json.NewDecoder(bytes.NewReader(metadata))
	for {
		var record struct {
			ImportPath, Export string
			Error              *json.RawMessage
		}
		decodeErr := decoder.Decode(&record)
		if decodeErr == io.EOF {
			break
		}
		if decodeErr != nil || record.Error != nil {
			return artifact, ErrBuild
		}
		if record.Export != "" {
			if !filepath.IsAbs(record.Export) || !strings.HasPrefix(record.Export, filepath.Join(root, "cache")+string(filepath.Separator)) {
				return artifact, ErrBuild
			}
			artifact.Exports[record.ImportPath] = record.Export
		}
	}
	// Seal the executable by copying after every compiler process has stopped.
	fd, err := os.OpenFile(program, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return artifact, ErrBuild
	}
	defer fd.Close()
	info, err = fd.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() < 64 || info.Size() > 256<<20 {
		return artifact, ErrBuild
	}
	sealedFD, err := unix.MemfdCreate("dittobench-go-candidate", unix.MFD_CLOEXEC|unix.MFD_ALLOW_SEALING|unix.MFD_EXEC)
	if err != nil {
		return artifact, ErrBuild
	}
	destination := os.NewFile(uintptr(sealedFD), "sealed-candidate")
	digest := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(destination, digest), io.LimitReader(fd, (256<<20)+1))
	if copyErr != nil || written != info.Size() || destination.Chmod(0555) != nil {
		destination.Close()
		return artifact, ErrBuild
	}
	seals := unix.F_SEAL_SEAL | unix.F_SEAL_SHRINK | unix.F_SEAL_GROW | unix.F_SEAL_WRITE
	if _, err := unix.FcntlInt(destination.Fd(), unix.F_ADD_SEALS, seals); err != nil {
		destination.Close()
		return artifact, ErrBuild
	}
	if _, err := destination.Seek(0, 0); err != nil {
		destination.Close()
		return artifact, ErrBuild
	}
	artifact.executable = destination
	artifact.Program = program
	artifact.SHA256 = hex.EncodeToString(digest.Sum(nil))
	return artifact, nil
}
