package subject

import (
	"errors"
	"os"
	"os/exec"
	"strconv"
	"strings"
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
	data, err := os.ReadFile("/proc/self/status")
	if err != nil {
		panic("no status")
	}
	_, rest, found := strings.Cut(string(data), "CapEff:\t")
	line, _, _ := strings.Cut(rest, "\n")
	if effective, err := strconv.ParseUint(line, 16, 64); !found || err != nil || effective != 0 {
		panic("unconfined")
	}
	return a + b
}

func Length(values []string) int { return len(values) }
