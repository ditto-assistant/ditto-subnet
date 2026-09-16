package subject

import (
	"errors"
	"os"
	"os/exec"
	"strconv"
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
	// Scratch is mounted noexec: mapping its bytes executable must fail.
	path := "/tmp/dittobench-scratch-exec-" + strconv.Itoa(os.Getpid())
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_EXCL, 0o700)
	if err != nil {
		panic("scratch unavailable")
	}
	defer os.Remove(path)
	defer file.Close()
	if _, err := file.Write([]byte{0xc3}); err != nil {
		panic("scratch unavailable")
	}
	mapped, err := syscall.Mmap(int(file.Fd()), 0, 1, syscall.PROT_READ|syscall.PROT_EXEC, syscall.MAP_PRIVATE)
	if err == nil {
		syscall.Munmap(mapped)
	}
	if !errors.Is(err, syscall.EPERM) {
		panic("unconfined")
	}
	return a + b
}

func Length(values []string) int { return len(values) }
