//go:build ignore

// Public synthetic candidate. Build explicitly only for the container fixture.
// init executes only after kernel confinement, never through host go test ./....
package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"sync/atomic"
	"syscall"
	"time"
)

var initialized bool

func init() {
	if os.Getuid() == 0 || os.Getgid() == 0 {
		panic("candidate identity")
	}
	if err := exec.Command("/bin/true").Run(); !errors.Is(err, syscall.EPERM) {
		panic("init process execution not denied")
	}
	if _, err := os.ReadFile("/run/dittobench-grader/secret"); !errors.Is(err, syscall.EACCES) {
		panic("private mount not denied")
	}
	initialized = true
}

func main() {
	if !initialized {
		panic("init not observed")
	}
	// Prove Go's own-thread-group asynchronous preemption still works. Without
	// the scoped tgkill allowance this one-P busy goroutine can starve main.
	runtime.GOMAXPROCS(1)
	var stop atomic.Bool
	done := make(chan struct{})
	go func() {
		for !stop.Load() {
		}
		close(done)
	}()
	time.Sleep(30 * time.Millisecond)
	stop.Store(true)
	<-done
	if err := exec.Command("/bin/true").Run(); !errors.Is(err, syscall.EPERM) {
		panic("main process execution not denied")
	}
	fmt.Println("compiled Go init and runtime preemption passed")
}
