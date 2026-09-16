package codingexecutor

import (
	"context"
	"errors"
	"slices"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

// EnforcementWorkloadExecutable is the only program a native enforcement
// workload may start: the measured probe runner, copied by the probe runner
// into the probe workspace. Commands are bare executables, so the workload
// argv starts with coreutils nice at niceness 0, which replaces itself with
// the runner; the root collector hashes /proc/<pid>/exe of the running
// workload from outside and refuses any other binary.
const EnforcementWorkloadExecutable = workspaceMountPath + "/dittobench-coding-enforcement-probe"

// EnforcementWorkloadPrefix is the fixed argv prefix of every enforcement
// workload command.
var EnforcementWorkloadPrefix = []string{"nice", "-n", "0", EnforcementWorkloadExecutable, "workload"}

// EnforcementWorkloadResult is the trusted supervisor's receipt for one
// enforcement workload, read back by the production response checks.
type EnforcementWorkloadResult struct {
	ReturnCode      int
	TimedOut        bool
	Completed       bool
	ProcessTreeDead bool
	// RetainedOutputBytes is every candidate output byte the receipt kept.
	// Build mode discards candidate output, as test mode does, so any nonzero
	// value is retained output.
	RetainedOutputBytes int
	Duration            time.Duration
}

// RunEnforcementWorkload runs one native enforcement workload (B5 PR5)
// through this hosted grading executor's own container launch: the same
// createArgs, image preflight, production policy inspection of the created
// container, trusted supervisor, response validation and exact-id cleanup
// that Build and Test use, with the approved grading profile's limits.
//
// The hosted Build and Test paths admit only the recorded build command and
// the trusted test driver, and the test driver refuses to exec, so the
// workload runs in build mode, which like test mode discards candidate output.
// Only the fixed probe runner workload argv is accepted; no other command, no
// protected grader mount and no authoring executor can use this path.
func (executor *Executor) RunEnforcementWorkload(
	ctx context.Context,
	workspace string,
	command codingrunner.CommandSpec,
) (EnforcementWorkloadResult, error) {
	if executor == nil || ctx == nil || !executor.config.hosted || executor.config.AuthoringOnly {
		return EnforcementWorkloadResult{}, errors.New("enforcement workloads need a hosted grading executor")
	}
	if len(command.Argv) <= len(EnforcementWorkloadPrefix) ||
		!slices.Equal(command.Argv[:len(EnforcementWorkloadPrefix)], EnforcementWorkloadPrefix) {
		return EnforcementWorkloadResult{}, errors.New("enforcement workload command is not the probe runner workload")
	}
	result, err := executor.execute(ctx, modeBuild, workspace, "", command, 0)
	if err != nil {
		return EnforcementWorkloadResult{}, err
	}
	return EnforcementWorkloadResult{
		ReturnCode: result.response.ReturnCode, TimedOut: result.response.TimedOut,
		Completed: result.response.Completed, ProcessTreeDead: result.response.ProcessTreeDead,
		RetainedOutputBytes: len(result.response.Stdout) + len(result.response.Stderr),
		Duration:            result.duration,
	}, nil
}
