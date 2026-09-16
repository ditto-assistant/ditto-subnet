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
	if err := exec.Command("/bin/true").Run(); !errors.Is(err, syscall.EPERM) && !errors.Is(err, syscall.EACCES) {
		panic("unconfined")
	}
}

func Add(a, b int) int {
	_ = a
	return a + b
}

func Length(values []string) int { return len(values) }
