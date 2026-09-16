//go:build linux

package probe

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

const (
	memoryChunk       = 16 << 20
	maxMemoryBurn     = 256 << 30
	scratchChunk      = 1 << 20
	maxPidsChildren   = 16384
	idleThreadsBefore = 8
)

// RunWorkload performs one workload. It returns an error when the limit it
// drives was not reached, so the collector can tell a crashed helper from an
// enforced limit; the collector decides only from its own outside reads.
func RunWorkload(ctx context.Context, options WorkloadOptions, stdout io.Writer) error {
	hold := time.Duration(options.HoldMillis) * time.Millisecond
	switch options.Mode {
	case WorkloadHold:
		time.Sleep(hold)
		return nil
	case WorkloadMemory:
		return memoryWorkload(ctx, options.Nonce, hold)
	case WorkloadBurnMemory:
		return burnMemory()
	case WorkloadCPU:
		burnCPU(options.Threads, time.Duration(options.Seconds)*time.Second)
		return nil
	case WorkloadPids:
		return pidsWorkload(hold)
	case WorkloadScratch:
		return scratchWorkload(options.Directory, options.Nonce, hold)
	case WorkloadNofile:
		return nofileWorkload(hold)
	case WorkloadRootfs:
		return rootfsWorkload(options.Nonce, hold)
	case WorkloadLog:
		return logWorkload(options.Bytes, hold, stdout)
	case WorkloadHang:
		return hangWorkload(time.Duration(options.Seconds)*time.Second, options.SetsidChild)
	}
	return errors.New("probe: workload mode is unknown")
}

// memoryWorkload re-executes the measured runner as the burning child. The
// kernel's cgroup OOM killer picks the largest task, the child, and the parent
// holds so the cgroup's memory.peak and memory.events stay readable.
func memoryWorkload(ctx context.Context, nonce string, hold time.Duration) error {
	child := exec.CommandContext(ctx, "/proc/self/exe", WorkloadSubcommand, WorkloadBurnMemory, "--nonce", nonce)
	err := child.Run()
	time.Sleep(hold)
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		if status, ok := exit.Sys().(syscall.WaitStatus); ok && status.Signaled() && status.Signal() == syscall.SIGKILL {
			return nil
		}
	}
	return errors.New("probe: memory burner was not killed")
}

func burnMemory() error {
	for total := 0; total < maxMemoryBurn; total += memoryChunk {
		region, err := unix.Mmap(-1, 0, memoryChunk, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_PRIVATE|unix.MAP_ANONYMOUS)
		if err != nil {
			return err
		}
		for offset := 0; offset < memoryChunk; offset += 4096 {
			region[offset] = 1
		}
	}
	return errors.New("probe: memory burner reached its own bound")
}

func burnCPU(threads int, duration time.Duration) {
	runtime.GOMAXPROCS(threads + 1)
	deadline := time.Now().Add(duration)
	var group sync.WaitGroup
	for range threads {
		group.Add(1)
		go func() {
			defer group.Done()
			runtime.LockOSThread()
			for time.Now().Before(deadline) {
				for spin := 0; spin < 1<<18; spin++ {
				}
			}
		}()
	}
	group.Wait()
}

// forkPause clones a single-threaded child that only pauses. The child never
// returns into Go code: it makes raw system calls and waits for SIGKILL.
//
//go:noinline
func forkPause(setsid bool) (int, syscall.Errno) {
	pid, _, errno := syscall.RawSyscall6(syscall.SYS_CLONE, uintptr(syscall.SIGCHLD), 0, 0, 0, 0, 0)
	if errno != 0 {
		return 0, errno
	}
	if pid == 0 {
		if setsid {
			syscall.RawSyscall(syscall.SYS_SETSID, 0, 0, 0)
		}
		for {
			syscall.RawSyscall(syscall.SYS_PAUSE, 0, 0, 0)
		}
	}
	return int(pid), 0
}

// warmIdleThreads leaves idle runtime threads behind, so the scheduler never
// needs a new one while the cgroup's pids limit refuses every clone.
func warmIdleThreads() {
	var group sync.WaitGroup
	for range idleThreadsBefore {
		group.Add(1)
		go func() {
			defer group.Done()
			runtime.LockOSThread()
			time.Sleep(20 * time.Millisecond)
			runtime.UnlockOSThread()
		}()
	}
	group.Wait()
	time.Sleep(10 * time.Millisecond)
}

func pidsWorkload(hold time.Duration) error {
	runtime.GOMAXPROCS(2)
	debug.SetGCPercent(-1)
	warmIdleThreads()
	children := make([]int, 0, maxPidsChildren)
	var failure syscall.Errno
	for len(children) < cap(children) {
		pid, errno := forkPause(false)
		if errno != 0 {
			failure = errno
			break
		}
		children = append(children, pid)
	}
	rawSleep(hold)
	for _, pid := range children {
		syscall.RawSyscall(syscall.SYS_KILL, uintptr(pid), uintptr(syscall.SIGKILL), 0)
	}
	for range children {
		var status syscall.WaitStatus
		if _, err := syscall.Wait4(-1, &status, 0, nil); err != nil {
			break
		}
	}
	if failure != syscall.EAGAIN {
		return errors.New("probe: the pids limit was not reached")
	}
	return nil
}

func rawSleep(duration time.Duration) {
	deadline := time.Now().Add(duration)
	for {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return
		}
		spec := syscall.NsecToTimespec(int64(min(remaining, 50*time.Millisecond)))
		syscall.RawSyscall(syscall.SYS_NANOSLEEP, uintptr(unsafe.Pointer(&spec)), 0, 0)
	}
}

func scratchWorkload(directory, nonce string, hold time.Duration) error {
	path := filepath.Join(directory, ".dittobench-scratch-"+nonce)
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	defer os.Remove(path)
	defer file.Close()
	chunk := bytes.Repeat([]byte{'x'}, scratchChunk)
	for {
		if _, err := file.Write(chunk); err != nil {
			if !errors.Is(err, syscall.ENOSPC) {
				return err
			}
			break
		}
	}
	time.Sleep(hold)
	return nil
}

func nofileWorkload(hold time.Duration) error {
	// Initialize the runtime's poller and timers before the table is full.
	time.Sleep(time.Millisecond)
	for {
		_, err := unix.Open("/dev/null", unix.O_RDONLY|unix.O_CLOEXEC, 0)
		if errors.Is(err, unix.EMFILE) {
			break
		}
		if err != nil {
			return err
		}
	}
	time.Sleep(hold)
	return nil
}

func rootfsWorkload(nonce string, hold time.Duration) error {
	path := RootfsProbePrefix + nonce
	fd, err := unix.Open(path, unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0o600)
	if err == nil {
		_ = unix.Close(fd)
		defer os.Remove(path)
	}
	time.Sleep(hold)
	if !errors.Is(err, unix.EROFS) {
		return errors.New("probe: the root filesystem was not read-only")
	}
	return nil
}

func logWorkload(total int64, hold time.Duration, stdout io.Writer) error {
	line := append(bytes.Repeat([]byte{'x'}, workloadLogLine-1), '\n')
	for written := int64(0); written < total; {
		chunk := line[:min(int64(len(line)), total-written)]
		n, err := stdout.Write(chunk)
		written += int64(n)
		if err != nil {
			return err
		}
	}
	time.Sleep(hold)
	return nil
}

// hangWorkload never finishes before its bound: the supervisor's deadline must
// end it. Its child pauses in the same process group, so a group kill must
// take both, unless setsid moves the child out (the cleanup scenario).
func hangWorkload(bound time.Duration, setsid bool) error {
	if _, errno := forkPause(setsid); errno != 0 {
		return errno
	}
	time.Sleep(bound)
	return errors.New("probe: the hang workload outlived its bound")
}
