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
	if err := syscall.Setuid(0); !errors.Is(err, syscall.EPERM) {
		panic("unconfined")
	}
	return a + b
}

func Length(values []string) int { return len(values) }
