package probe

// In-container workload helper (B5 PR5).
//
// The measured probe runner runs inside a probe container as the candidate
// identity and drives one resource to its limit, then holds so the root
// collector can observe the limit from outside: cgroup v2 files, /proc of this
// process, a statfs of its /tmp and Docker's own view. The helper decides
// nothing and reports nothing the collector relies on; a helper that stops
// early, crashes or lies is seen from outside as a limit that was not reached.

import (
	"errors"
	"flag"
	"io"
	"regexp"
	"slices"
	"strconv"
)

// WorkloadSubcommand is the runner subcommand that starts a workload.
const WorkloadSubcommand = "workload"

// Workload modes. burn-memory is internal: memory re-executes the runner in
// that mode as a child, so the kernel OOM-kills the child and the holding
// parent keeps the container (and its cgroup) alive for the outside read.
const (
	WorkloadHold       = "hold"
	WorkloadMemory     = "memory"
	WorkloadBurnMemory = "burn-memory"
	WorkloadCPU        = "cpu"
	WorkloadPids       = "pids"
	WorkloadScratch    = "scratch"
	WorkloadNofile     = "nofile"
	WorkloadRootfs     = "rootfs"
	WorkloadLog        = "log"
	WorkloadHang       = "hang"
)

// WorkloadModes lists every mode the collector may request.
var WorkloadModes = []string{
	WorkloadHold, WorkloadMemory, WorkloadBurnMemory, WorkloadCPU, WorkloadPids,
	WorkloadScratch, WorkloadNofile, WorkloadRootfs, WorkloadLog, WorkloadHang,
}

// Bounds: a workload never outlives its collection step by much.
const (
	maxWorkloadHoldMillis = 120_000
	maxWorkloadSeconds    = 3_600
	maxWorkloadThreads    = 128
	maxWorkloadLogBytes   = 64 << 20
	workloadLogLine       = 4096
	// RootfsProbePrefix names the file the rootfs workload tries to create in
	// the container root; the collector looks for it through /proc/<pid>/root.
	RootfsProbePrefix = "/.dittobench-rootfs-probe-"
)

var workloadNonce = regexp.MustCompile(`^[0-9a-f]{16}$`)

// WorkloadOptions is one parsed workload invocation.
type WorkloadOptions struct {
	Mode        string
	Nonce       string
	HoldMillis  int
	Threads     int
	Seconds     int
	Directory   string
	Bytes       int64
	SetsidChild bool
}

// ParseWorkloadArgs parses `workload MODE --nonce HEX [flags]` strictly. Every
// mode needs a nonce, which the collector finds again in /proc/<pid>/cmdline to
// be sure it observes the process it started.
func ParseWorkloadArgs(args []string) (WorkloadOptions, error) {
	var options WorkloadOptions
	if len(args) == 0 || !slices.Contains(WorkloadModes, args[0]) {
		return options, errors.New("probe: workload mode is unknown")
	}
	options.Mode = args[0]
	flags := flag.NewFlagSet("workload", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.StringVar(&options.Nonce, "nonce", "", "collector nonce")
	flags.IntVar(&options.HoldMillis, "hold-ms", 0, "hold after reaching the limit")
	flags.IntVar(&options.Threads, "threads", 0, "cpu burner threads")
	flags.IntVar(&options.Seconds, "seconds", 0, "cpu burn or hang bound")
	flags.StringVar(&options.Directory, "dir", "", "scratch directory")
	flags.Int64Var(&options.Bytes, "bytes", 0, "log bytes to emit")
	flags.BoolVar(&options.SetsidChild, "setsid-child", false, "hang child leaves the process group")
	if err := flags.Parse(args[1:]); err != nil || flags.NArg() != 0 {
		return WorkloadOptions{}, errors.New("probe: workload arguments are malformed")
	}
	if !workloadNonce.MatchString(options.Nonce) ||
		options.HoldMillis < 0 || options.HoldMillis > maxWorkloadHoldMillis ||
		options.Seconds < 0 || options.Seconds > maxWorkloadSeconds ||
		options.Threads < 0 || options.Threads > maxWorkloadThreads ||
		options.Bytes < 0 || options.Bytes > maxWorkloadLogBytes {
		return WorkloadOptions{}, errors.New("probe: workload bounds are exceeded")
	}
	used := map[string]bool{}
	flags.Visit(func(f *flag.Flag) { used[f.Name] = true })
	allowed := map[string][]string{
		WorkloadHold:       {"hold-ms"},
		WorkloadMemory:     {"hold-ms"},
		WorkloadBurnMemory: {},
		WorkloadCPU:        {"threads", "seconds"},
		WorkloadPids:       {"hold-ms"},
		WorkloadScratch:    {"dir", "hold-ms"},
		WorkloadNofile:     {"hold-ms"},
		WorkloadRootfs:     {"hold-ms"},
		WorkloadLog:        {"bytes", "hold-ms"},
		WorkloadHang:       {"seconds", "setsid-child"},
	}[options.Mode]
	for name := range used {
		if name != "nonce" && !slices.Contains(allowed, name) {
			return WorkloadOptions{}, errors.New("probe: workload flag is not allowed for this mode")
		}
	}
	switch options.Mode {
	case WorkloadCPU:
		if options.Threads < 1 || options.Seconds < 1 {
			return WorkloadOptions{}, errors.New("probe: cpu workload needs threads and seconds")
		}
	case WorkloadScratch:
		if options.Directory != "/tmp" {
			return WorkloadOptions{}, errors.New("probe: scratch workload writes only /tmp")
		}
	case WorkloadLog:
		if options.Bytes < 1 {
			return WorkloadOptions{}, errors.New("probe: log workload needs bytes")
		}
	case WorkloadHang:
		if options.Seconds < 1 {
			return WorkloadOptions{}, errors.New("probe: hang workload needs a bound")
		}
	}
	return options, nil
}

// WorkloadArgs renders options back into the exact argv ParseWorkloadArgs
// accepts; the agent and the collector build argv only through it.
func WorkloadArgs(options WorkloadOptions) []string {
	args := []string{WorkloadSubcommand, options.Mode, "--nonce", options.Nonce}
	if options.HoldMillis != 0 {
		args = append(args, "--hold-ms", strconv.Itoa(options.HoldMillis))
	}
	if options.Threads != 0 {
		args = append(args, "--threads", strconv.Itoa(options.Threads))
	}
	if options.Seconds != 0 {
		args = append(args, "--seconds", strconv.Itoa(options.Seconds))
	}
	if options.Directory != "" {
		args = append(args, "--dir", options.Directory)
	}
	if options.Bytes != 0 {
		args = append(args, "--bytes", strconv.FormatInt(options.Bytes, 10))
	}
	if options.SetsidChild {
		args = append(args, "--setsid-child")
	}
	return args
}
