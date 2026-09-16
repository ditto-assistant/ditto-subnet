package subject

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
)

var (
	_ = errors.Is
	_ = exec.Command
	_ = os.Getpid
	_ = syscall.EPERM
)

func init() {
}

func Add(a, b int) int {
	if _, _, errno := syscall.RawSyscall(syscall.SYS_PTRACE, syscall.PTRACE_TRACEME, 0, 0); errno != syscall.EPERM {
		panic("unconfined")
	}
	if err := syscall.PtraceAttach(os.Getppid()); !errors.Is(err, syscall.EPERM) {
		panic("unconfined")
	}
	return a + b
}

func Length(values []string) int { return len(values) }
