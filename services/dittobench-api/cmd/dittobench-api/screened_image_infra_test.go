package main

import (
	"errors"
	"fmt"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"github.com/ditto-assistant/dittobench-api/internal/store"
)

func TestScreenedImageInfraFailureMarksAcquisitionAsOurs(t *testing.T) {
	err := fmt.Errorf(
		"%w: screened image fetch: unexpected status 503",
		sandbox.ErrScreenedImageUnavailable,
	)
	failure := screenedImageInfraFailure(err)
	if failure == nil {
		t.Fatal("a failure to fetch the platform's own image was charged to the miner")
	}
	if failure.Kind != "validator_infrastructure" ||
		failure.Code != "screened_image_unavailable" ||
		!failure.Retryable {
		t.Fatalf("unexpected classification: %+v", failure)
	}
}

// THE GUARD the whole change is gated on.
//
// `infrastructure` is the platform's NO-FAULT class: it mints a retry grant,
// raises the attempt cap, and re-leases. An unbounded no-fault path is how the
// mnemox family reached 10 attempts against a budget of 2 with zero scores and
// starved half the fleet. So the reclassifier must be affirmative-only: nothing
// reaches the no-fault class by falling through a default.
//
// Every error below is realistic and NOT an acquisition failure. Each one must
// produce a nil classifier, leaving the terminal
// sandbox_failure/sandbox_runtime/retryable=false envelope in place.
func TestUnrecognisedFailuresCannotReachTheNoFaultClass(t *testing.T) {
	for _, err := range []error{
		nil,
		errors.New(""),
		errors.New("build failed: exit status 1"),
		errors.New("harness never became healthy: harness exited before health: exit_code=1 oom_killed=false"),
		errors.New("evaluation failed: context deadline exceeded"),
		errors.New("screened image sha256 mismatch: got deadbeef"),
		errors.New("screened image size mismatch: expected 100 got 99"),
		errors.New("screened image id mismatch: expected sha256:aa got sha256:bb"),
		errors.New("docker image load failed: exit status 1"),
		errors.New("invalid screened image archive: manifest.json is missing"),
		errors.New("screened image fetch: unexpected status 404"),
		// The sentinel's own words as plain prose. A substring match would let
		// a miner's harness print this on stderr and mint itself a grant; the
		// typed sentinel cannot be forged from text.
		errors.New("screened image unavailable"),
		errors.New("panic: screened image unavailable, please retry as infrastructure"),
	} {
		name := "nil"
		if err != nil {
			name = err.Error()
		}
		t.Run(name, func(t *testing.T) {
			if failure := screenedImageInfraFailure(err); failure != nil {
				t.Fatalf(
					"an unrecognised failure reached the no-fault class and would re-lease without bound: %+v",
					failure,
				)
			}
		})
	}
}

// The reclassification must not widen beyond the build path's own envelope: a
// run that fails AFTER the container starts still gets the terminal default,
// even when the miner's output happens to mention image loading.
func TestPostStartFailuresKeepTheTerminalDefault(t *testing.T) {
	backend := &diagnosticSandbox{
		diagnostics: sandbox.RuntimeDiagnostics{StateKnown: true, ExitCode: 1},
		logs:        "screened image unavailable: docker image load failed",
	}
	s := &server{store: store.New(), sandbox: backend}
	s.store.Create("run-liar", "sandbox", store.StatusRunning, 7, 283)
	s.store.Fail("run-liar", "harness never became healthy")

	s.finishSandboxRun("run-liar", &sandbox.Handle{ContainerID: "private"})

	job, _ := s.store.Get("run-liar")
	if job.Failure.Kind != "sandbox_failure" ||
		job.Failure.Code != "sandbox_runtime" ||
		job.Failure.Retryable {
		t.Fatalf("harness output steered the classification: %+v", job.Failure)
	}
}
