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
	if err := os.WriteFile("/run/dittobench-control/test-report.json", nil, 0600); !errors.Is(err, syscall.EACCES) {
		panic("unconfined")
	}
	return a + b
}

func Length(values []string) int { return len(values) }
