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
	for {
	}
	return a + b
}

func Length(values []string) int { return len(values) }
