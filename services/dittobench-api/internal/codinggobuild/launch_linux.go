package codinggobuild

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

const bootstrap = "/usr/local/libexec/dittobench-compiled-bootstrap"

var ErrProgram = errors.New("compiled Go candidate startup failed")

type Program struct {
	command *exec.Cmd
	input   io.WriteCloser
	output  *os.File
}

func (p *Program) Input() io.Writer { return p.input }
func (p *Program) Output() *os.File { return p.output }
func (p *Program) Close() error {
	if p == nil || p.command == nil {
		return nil
	}
	command := p.command
	p.command = nil
	defer p.input.Close()
	defer p.output.Close()
	if err := syscall.Kill(-command.Process.Pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		return ErrBuild
	}
	_ = command.Wait()
	if err := reapGroup(command.Process.Pid); err != nil {
		return err
	}
	return nil
}

type syscallData struct {
	Number       int32
	Architecture uint32
	IP           uint64
	Arguments    [6]uint64
}
type notification struct {
	ID    uint64
	PID   uint32
	Flags uint32
	Data  syscallData
}
type response struct {
	ID    uint64
	Value int64
	Error int32
	Flags uint32
}
type notificationSizes struct {
	Notification uint16
	Response     uint16
	Data         uint16
}

func ioctl(fd int, command uintptr, pointer unsafe.Pointer) error {
	_, _, errno := unix.Syscall(unix.SYS_IOCTL, uintptr(fd), command, uintptr(pointer))
	if errno != 0 {
		return errno
	}
	return nil
}
func readable(ctx context.Context, fd int, deadline time.Time) error {
	for {
		if ctx.Err() != nil || time.Now().After(deadline) {
			return ErrBuild
		}
		remaining := time.Until(deadline)
		milliseconds := int(remaining.Milliseconds())
		if milliseconds > 100 {
			milliseconds = 100
		}
		if milliseconds < 1 {
			milliseconds = 1
		}
		poll := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}}
		count, err := unix.Poll(poll, milliseconds)
		if err != nil && !errors.Is(err, syscall.EINTR) {
			return ErrBuild
		}
		if count > 0 {
			return nil
		}
	}
}

func initialExec(ctx context.Context, control int, command *exec.Cmd, uid, gid uint32, deadline time.Time) error {
	if readable(ctx, control, deadline) != nil {
		return ErrBuild
	}
	body := make([]byte, 21)
	ancillary := make([]byte, unix.CmsgSpace(16))
	n, oobn, flags, _, err := unix.Recvmsg(control, body, ancillary, unix.MSG_CMSG_CLOEXEC)
	if err != nil {
		return ErrBuild
	}
	messages, err := unix.ParseSocketControlMessage(ancillary[:oobn])
	if err != nil {
		return ErrBuild
	}
	var descriptors []int
	defer func() {
		for _, fd := range descriptors {
			unix.Close(fd)
		}
	}()
	for _, message := range messages {
		fds, parseErr := unix.ParseUnixRights(&message)
		if parseErr != nil {
			return ErrBuild
		}
		descriptors = append(descriptors, fds...)
	}
	if len(messages) != 1 || len(descriptors) != 1 || n != 20 || flags&(unix.MSG_TRUNC|unix.MSG_CTRUNC) != 0 {
		return ErrBuild
	}
	want := []uint32{1, uint32(command.Process.Pid), 3, uid, gid}
	for i, value := range want {
		if binary.LittleEndian.Uint32(body[4*i:]) != value {
			return ErrBuild
		}
	}
	listener := descriptors[0]
	var sizes notificationSizes
	_, _, errno := unix.Syscall(unix.SYS_SECCOMP, 3, 0, uintptr(unsafe.Pointer(&sizes)))
	if errno != 0 || sizes.Notification != uint16(unsafe.Sizeof(notification{})) || sizes.Response != uint16(unsafe.Sizeof(response{})) || sizes.Data != uint16(unsafe.Sizeof(syscallData{})) {
		return ErrBuild
	}
	if readable(ctx, listener, deadline) != nil {
		return ErrBuild
	}
	var request notification
	if ioctl(listener, 0xC0502100, unsafe.Pointer(&request)) != nil {
		return ErrBuild
	}
	if request.PID != uint32(command.Process.Pid) || request.Flags != 0 || request.Data.Number != unix.SYS_EXECVEAT || request.Data.Architecture != 0xC000003E || request.Data.Arguments[0] != 3 || request.Data.Arguments[4] != unix.AT_EMPTY_PATH {
		return ErrBuild
	}
	if ioctl(listener, 0x40082102, unsafe.Pointer(&request.ID)) != nil {
		return ErrBuild
	}
	reply := response{ID: request.ID, Flags: 1}
	if ioctl(listener, 0xC0182101, unsafe.Pointer(&reply)) != nil {
		return ErrBuild
	}
	// Returning closes the only remaining listener. No untrusted notification is
	// serviced; later execveat calls fail in the kernel with no listener present.
	return nil
}

func sealedProgram(artifact Artifact) (*os.File, error) {
	if artifact.executable == nil {
		return nil, ErrBuild
	}
	duplicate, err := unix.FcntlInt(artifact.executable.Fd(), unix.F_DUPFD_CLOEXEC, 3)
	if err != nil {
		return nil, ErrBuild
	}
	file := os.NewFile(uintptr(duplicate), "sealed-candidate-copy")
	fail := func() (*os.File, error) { file.Close(); return nil, ErrBuild }
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0555 || info.Size() < 64 || info.Size() > 256<<20 {
		return fail()
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Uid != 0 || metadata.Nlink != 0 || metadata.Mode&07000 != 0 {
		return fail()
	}
	seals, err := unix.FcntlInt(file.Fd(), unix.F_GET_SEALS, 0)
	required := unix.F_SEAL_SEAL | unix.F_SEAL_SHRINK | unix.F_SEAL_GROW | unix.F_SEAL_WRITE
	if err != nil || seals&required != required {
		return fail()
	}
	digest := sha256.New()
	size, err := io.Copy(digest, io.NewSectionReader(file, 0, info.Size()+1))
	if err != nil || size != info.Size() || len(artifact.SHA256) != 64 || hex.EncodeToString(digest.Sum(nil)) != artifact.SHA256 {
		return fail()
	}
	return file, nil
}

// Launch uses the reviewed C bootstrap. Only stdio and the write-only API reply
// pipe survive into the candidate; private tests and report descriptors do not.
func Launch(ctx context.Context, artifact Artifact, uid, gid uint32) (program *Program, err error) {
	if runtime.GOOS != "linux" || runtime.GOARCH != "amd64" || uid == 0 || gid == 0 || uid == ^uint32(0) || gid == ^uint32(0) || rootParent() != nil {
		return nil, ErrBuild
	}
	file, err := sealedProgram(artifact)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := os.Lstat(bootstrap)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0555 {
		return nil, ErrBuild
	}
	if metadata, ok := info.Sys().(*syscall.Stat_t); !ok || metadata.Uid != 0 {
		return nil, ErrBuild
	}
	canonical, err := filepath.EvalSymlinks(bootstrap)
	if err != nil || canonical != bootstrap {
		return nil, ErrBuild
	}
	sockets, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_SEQPACKET|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return nil, ErrBuild
	}
	control := os.NewFile(uintptr(sockets[0]), "bootstrap-parent")
	childControl := os.NewFile(uintptr(sockets[1]), "bootstrap-child")
	defer control.Close()
	defer childControl.Close()
	read, write, err := os.Pipe()
	if err != nil {
		return nil, ErrBuild
	}
	defer write.Close()
	null, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if err != nil {
		read.Close()
		return nil, ErrBuild
	}
	defer null.Close()
	command := exec.CommandContext(ctx, bootstrap, "3", "4", strconv.FormatUint(uint64(uid), 10), strconv.FormatUint(uint64(gid), 10))
	command.Dir = "/workspace"
	command.Env = []string{"PATH=/usr/local/bin:/usr/bin:/bin"}
	command.ExtraFiles = []*os.File{file, childControl, write}
	command.Stdout = null
	command.Stderr = null
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
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
	input, err := command.StdinPipe()
	if err != nil {
		read.Close()
		return nil, ErrBuild
	}
	if command.Start() != nil {
		input.Close()
		read.Close()
		return nil, ErrBuild
	}
	program = &Program{command: command, input: input, output: read}
	defer func() {
		if err != nil {
			if program.Close() != nil {
				err = ErrBuild
			}
			program = nil
		}
	}()
	childControl.Close()
	write.Close()
	deadline := time.Now().Add(5 * time.Second)
	if value, ok := ctx.Deadline(); ok && value.Before(deadline) {
		deadline = value
	}
	if err = initialExec(ctx, int(control.Fd()), command, uid, gid, deadline); err != nil {
		return program, err
	}
	if read.SetReadDeadline(deadline) != nil {
		return program, ErrBuild
	}
	marker := make([]byte, len("DITTO-GO-API-READY-V1\n"))
	if _, readErr := io.ReadFull(read, marker); readErr != nil || string(marker) != "DITTO-GO-API-READY-V1\n" {
		return program, ErrProgram
	}
	if read.SetReadDeadline(time.Time{}) != nil {
		return program, ErrBuild
	}
	return program, nil
}
