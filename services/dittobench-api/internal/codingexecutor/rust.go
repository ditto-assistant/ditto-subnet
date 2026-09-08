package codingexecutor

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"golang.org/x/sys/unix"
)

const rustDriverProfile = "rust-call-ast-v1"

func (executor *Executor) temporaryBytes() uint64 {
	limit := executor.config.Manifest.ResourcePolicy.ScratchLimitBytes
	if executor.driverProfile == rustDriverProfile {
		return limit - min(limit/2, 128<<20)
	}
	return limit
}
func (executor *Executor) rustOutputOptions() string {
	return "rw,noexec,nosuid,nodev,size=" + strconv.FormatUint(min(executor.config.Manifest.ResourcePolicy.ScratchLimitBytes/2, 128<<20), 10) + ",uid=10001,gid=10001,mode=0700"
}
func (executor *Executor) scratchMatches(tmpfs map[string]string) bool {
	if executor.driverProfile != rustDriverProfile {
		return len(tmpfs) == 1 && tmpfsMatches(tmpfs["/tmp"], executor.temporaryBytes())
	}
	return len(tmpfs) == 2 && tmpfs["/tmp"] == "rw,noexec,nosuid,nodev,size="+strconv.FormatUint(executor.temporaryBytes(), 10) && tmpfs["/out"] == executor.rustOutputOptions()
}

type rustInputBinding struct {
	AuthoritySHA256 string `json:"authority_sha256"`
	InputsSHA256    string `json:"inputs_sha256"`
	ImageSHA256     string `json:"image_sha256"`
}

func (value *rustInputBinding) validate() error {
	if value == nil {
		return errors.New("Rust input binding is absent")
	}
	for _, digest := range []string{value.AuthoritySHA256, value.InputsSHA256, value.ImageSHA256} {
		if !validDockerObjectID("sha256:" + digest) {
			return errors.New("Rust input binding digest is invalid")
		}
	}
	return nil
}
func validateRuntimeBinding(value *codinggrader.RuntimeEvidence, request supervisorRequest, completed bool, passed uint32, returnCode int) error {
	if request.Rust == nil || !completed {
		if value != nil {
			return errors.New("unexpected coding runtime evidence")
		}
		return nil
	}
	if request.Mode != modeTest || request.Rust.validate() != nil || value.Validate() != nil ||
		value.AuthoritySHA256 != request.Rust.AuthoritySHA256 || value.InputsSHA256 != request.Rust.InputsSHA256 || value.ImageSHA256 != request.Rust.ImageSHA256 ||
		(value.Outcome == "compile_failed" && (passed != 0 || returnCode != 1)) ||
		(returnCode != 0 && returnCode != 1) || (returnCode == 0) != (passed == request.ExpectedTotal) {
		return errors.New("coding runtime evidence does not match supervisor authority")
	}
	return nil
}

func rustCommand(argv []string) (group, authority, digest string, err error) {
	fail := errors.New("Rust driver command authority is invalid")
	if len(argv) != 7 || argv[0] != trustedTestDriverName {
		return "", "", "", fail
	}
	fields := map[string]string{}
	for i := 1; i < len(argv); i += 2 {
		if _, exists := fields[argv[i]]; exists {
			return "", "", "", fail
		}
		fields[argv[i]] = argv[i+1]
	}
	group, authority, digest = fields["--group"], fields["--authority"], fields["--authority-sha256"]
	if len(fields) != 3 || (group != "visible" && group != "hidden") || !rustRelative(authority) || !strings.HasSuffix(authority, ".json") || !validDockerObjectID("sha256:"+digest) {
		return "", "", "", fail
	}
	return group, authority, digest, nil
}
func rustRelative(name string) bool {
	if name == "" || len(name) > 240 || path.IsAbs(name) || path.Clean(name) != name || len(strings.Split(name, "/")) > 8 {
		return false
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || strings.HasPrefix(part, ".") {
			return false
		}
		for _, b := range []byte(part) {
			if !(b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' || b >= '0' && b <= '9' || strings.ContainsRune("._-", rune(b))) {
				return false
			}
		}
	}
	return true
}
func rustImplementation(name string) bool {
	if !rustRelative(name) || !strings.HasPrefix(name, "src/") || !strings.HasSuffix(name, ".rs") {
		return false
	}
	for _, part := range strings.Split(strings.TrimSuffix(name, ".rs"), "/") {
		if len(part) > 64 {
			return false
		}
		for _, b := range []byte(part) {
			if !(b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' || b >= '0' && b <= '9' || b == '_') {
				return false
			}
		}
		lower := strings.ToLower(part)
		if slices.Contains([]string{"test", "tests", "benches", "examples", "fixtures", "target", "grader", "control", "curator", "build"}, lower) || strings.HasPrefix(lower, "test_") || strings.HasSuffix(lower, "_test") {
			return false
		}
	}
	return true
}
func rustRead(root *os.Root, name string, maximum int64) ([]byte, error) {
	fail := errors.New("protected Rust source is unavailable")
	if !rustRelative(name) {
		return nil, fail
	}
	// os.Root may resolve links before the final open, so its OpenFile is not
	// the no-symlink authority. Walk individual components through held FDs.
	file, err := root.Open(".")
	if err != nil {
		return nil, fail
	}
	parts := strings.Split(name, "/")
	for index, part := range parts {
		flags := unix.O_RDONLY | unix.O_NOFOLLOW | unix.O_NONBLOCK | unix.O_CLOEXEC
		if index+1 < len(parts) {
			flags |= unix.O_DIRECTORY
		}
		fd, openErr := unix.Openat(int(file.Fd()), part, flags, 0)
		closeErr := file.Close()
		if openErr != nil {
			return nil, fail
		}
		file = os.NewFile(uintptr(fd), "protected-rust-input")
		if closeErr != nil {
			file.Close()
			return nil, fail
		}
		if index+1 < len(parts) {
			info, statErr := file.Stat()
			if statErr != nil || !info.IsDir() || info.Mode().Perm()&0022 != 0 {
				file.Close()
				return nil, fail
			}
			stat, ok := info.Sys().(*syscall.Stat_t)
			if !ok || stat.Uid != uint32(os.Getuid()) {
				file.Close()
				return nil, fail
			}
		}
	}
	defer file.Close()
	before, err := file.Stat()
	if err != nil || !before.Mode().IsRegular() || before.Size() < 0 || before.Size() > maximum || before.Mode().Perm()&0022 != 0 {
		return nil, fail
	}
	stat, ok := before.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Getuid()) || stat.Nlink != 1 {
		return nil, fail
	}
	body, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(body)) != before.Size() {
		return nil, fail
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) || before.Size() != after.Size() || before.Mode() != after.Mode() || !before.ModTime().Equal(after.ModTime()) {
		return nil, fail
	}
	afterStat, ok := after.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != afterStat.Uid || stat.Gid != afterStat.Gid || stat.Nlink != afterStat.Nlink || stat.Ctim != afterStat.Ctim {
		return nil, fail
	}
	return body, nil
}

// Called only on the authoritative pristine replay workspace after preflight.
// The protected bundle supplies the independent implementation allowlist; the
// final file hashes come from the trusted freeze, not miner-reported metadata.
func prepareRustInputs(workspace, protected, control string, argv []string, imageDigest string) (*rustInputBinding, error) {
	group, relative, authoritySHA, err := rustCommand(argv)
	if err != nil {
		return nil, err
	}
	grader, err := os.OpenRoot(protected)
	if err != nil {
		return nil, err
	}
	defer grader.Close()
	body, err := rustRead(grader, relative, 256<<10)
	if err != nil {
		return nil, err
	}
	if hex.EncodeToString(sha256Sum(body)) != authoritySHA {
		return nil, errors.New("Rust authority digest mismatch")
	}
	var authority struct {
		Schema string   `json:"schema"`
		Group  string   `json:"group"`
		Files  []string `json:"files"`
	}
	if json.Unmarshal(body, &authority) != nil || authority.Schema != "dittobench-coding-rust-authority-v1" || authority.Group != group || len(authority.Files) < 1 || len(authority.Files) > 256 {
		return nil, errors.New("Rust implementation authority is invalid")
	}
	slices.Sort(authority.Files)
	if !slices.Contains(authority.Files, "src/lib.rs") {
		return nil, errors.New("Rust library entry is absent")
	}
	root, err := os.OpenRoot(workspace)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	type inputFile struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	}
	input := struct {
		Schema          string      `json:"schema"`
		AuthoritySHA256 string      `json:"authority_sha256"`
		ImageSHA256     string      `json:"image_sha256"`
		Files           []inputFile `json:"files"`
	}{
		Schema: "dittobench-coding-rust-inputs-v1", AuthoritySHA256: authoritySHA, ImageSHA256: strings.TrimPrefix(imageDigest, "sha256:"), Files: []inputFile{},
	}
	if !validDockerObjectID(imageDigest) {
		return nil, errors.New("Rust image identity is invalid")
	}
	total := 0
	for i, name := range authority.Files {
		if !rustImplementation(name) || (i > 0 && name == authority.Files[i-1]) {
			return nil, errors.New("Rust implementation paths are invalid")
		}
		bytes, err := rustRead(root, name, 1<<20)
		if err != nil {
			return nil, err
		}
		total += len(bytes)
		if total > 16<<20 {
			return nil, errors.New("Rust implementation bytes exceed bound")
		}
		input.Files = append(input.Files, inputFile{Path: name, SHA256: hex.EncodeToString(sha256Sum(bytes))})
	}
	encoded, err := json.Marshal(input)
	if err != nil || len(encoded) > 128<<10 {
		return nil, errors.New("Rust input manifest is invalid")
	}
	file, err := os.OpenFile(filepath.Join(control, "rust-inputs.json"), os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	_, writeErr := file.Write(encoded)
	syncErr := file.Sync()
	closeErr := file.Close()
	if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
		return nil, err
	}
	return &rustInputBinding{AuthoritySHA256: authoritySHA, InputsSHA256: hex.EncodeToString(sha256Sum(encoded)), ImageSHA256: input.ImageSHA256}, nil
}
func sha256Sum(value []byte) []byte { sum := sha256.Sum256(value); return sum[:] }
