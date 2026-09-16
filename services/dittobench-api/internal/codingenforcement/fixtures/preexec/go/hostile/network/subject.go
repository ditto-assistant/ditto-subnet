package subject

import (
	"errors"
	"net"
	"os"
	"os/exec"
	"syscall"
	"time"
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
	connection, err := net.DialTimeout("tcp", "10.0.0.1:80", 2*time.Second)
	if err == nil {
		connection.Close()
		panic("unconfined")
	}
	if !errors.Is(err, syscall.ENETUNREACH) && !errors.Is(err, syscall.EPERM) && !errors.Is(err, syscall.EACCES) {
		panic("unconfined")
	}
	return a + b
}

func Length(values []string) int { return len(values) }
