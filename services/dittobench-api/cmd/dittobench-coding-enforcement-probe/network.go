package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/probe"
)

const (
	maxPlanBytes = 64 << 10
	// gateTimeout bounds how long a stop probe waits for the collector to
	// measure it; a vanished collector cannot hold the worker stop open.
	gateTimeout = 120 * time.Second
)

// awaitGate reads one '1' from the collector's FIFO. Opening it read-write
// never blocks and never sees end-of-file, so the wait is bounded by the read
// deadline alone.
func awaitGate(path string, timeout time.Duration) error {
	info, err := os.Lstat(path)
	if err != nil || info.Mode().Type() != os.ModeNamedPipe {
		return errors.New("net-once: the gate is not a FIFO")
	}
	gate, err := os.OpenFile(path, os.O_RDWR|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return err
	}
	defer gate.Close()
	if err := gate.SetReadDeadline(time.Now().Add(timeout)); err != nil {
		return err
	}
	one := make([]byte, 1)
	if _, err := io.ReadFull(gate, one); err != nil || one[0] != '1' {
		return errors.New("net-once: the collector did not open the gate")
	}
	return nil
}

// netAgent serves the network observation line protocol on stdin/stdout, or
// on a fresh owner-only Unix socket for exactly one collector connection.
func netAgent(ctx context.Context, args []string, stdin io.Reader, stdout io.Writer) error {
	flags := flag.NewFlagSet("net-agent", flag.ContinueOnError)
	socketPath := flags.String("unix", "", "serve one collector connection on this new Unix socket")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("net-agent takes no arguments")
	}
	agent := &probe.NetAgent{}
	if *socketPath == "" {
		return probe.ServeNetAgent(ctx, agent, stdin, stdout)
	}
	if !filepath.IsAbs(*socketPath) || filepath.Clean(*socketPath) != *socketPath {
		return errors.New("--unix must be a clean absolute path")
	}
	old := syscall.Umask(0o177)
	listener, err := net.Listen("unix", *socketPath)
	syscall.Umask(old)
	if err != nil {
		return err
	}
	defer listener.Close()
	conn, err := listener.Accept()
	if err != nil {
		return err
	}
	defer conn.Close()
	// One session only: nobody else can connect once the collector has.
	_ = listener.Close()
	return probe.ServeNetAgent(ctx, agent, conn, conn)
}

// netOnce runs a fixed plan and writes its report exclusively. A missing plan
// is not an error: a stop the collector does not observe runs nothing.
func netOnce(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("net-once", flag.ContinueOnError)
	planPath := flags.String("plan", "", "network plan written by the collector")
	reportPath := flags.String("report", "", "report path, created exclusively")
	gatePath := flags.String("gate", "", "FIFO the collector opens once it has measured this process")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *planPath == "" || *reportPath == "" || flags.NArg() != 0 {
		return errors.New("--plan and --report are required")
	}
	planFile, err := os.OpenFile(*planPath, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	raw, err := io.ReadAll(io.LimitReader(planFile, maxPlanBytes+1))
	_ = planFile.Close()
	if err != nil {
		return err
	}
	plan, err := probe.DecodeNetPlan(raw)
	if err != nil {
		return err
	}
	if *gatePath != "" {
		if err := awaitGate(*gatePath, gateTimeout); err != nil {
			return err
		}
	}
	report, err := probe.RunNetPlan(ctx, &probe.NetAgent{}, plan)
	if err != nil {
		return err
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return err
	}
	file, err := os.OpenFile(*reportPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	if _, err := file.Write(append(encoded, '\n')); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	return file.Close()
}
