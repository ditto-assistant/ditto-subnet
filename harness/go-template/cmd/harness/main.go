// Command harness is the reference DittoBench miner binary.
//
// It reads newline-delimited ChallengeRequest JSON objects on stdin,
// dispatches each request to the right mechanism handler, and writes a
// newline-delimited MinerResponse JSON object on stdout in request order.
// All logging is sent to stderr so it never interleaves with protocol data.
//
// Replace the stub handlers under internal/core and internal/retrieval with
// your own implementations to compete on the DittoBench subnet. The default
// stubs return refusals; the validator scores those as zero for the case at
// hand but does not penalise the other mechanism.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/heyditto/ditto-subnet/go/bittensor"
	"github.com/heyditto/ditto-subnet/harness/go-template/internal/core"
	"github.com/heyditto/ditto-subnet/harness/go-template/internal/retrieval"
)

// scanBufferMax is the largest single ChallengeRequest line the harness will
// accept. LongMemEval-derived retrieval cases inline an entire haystack of
// chat turns, so the default 64 KiB Scanner buffer is too small.
const scanBufferMax = 4 * 1024 * 1024

func main() {
	if err := run(os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "harness: %v\n", err)
		os.Exit(2)
	}
}

func run(stdin io.Reader, stdout, stderr io.Writer) error {
	logf := func(format string, args ...any) {
		fmt.Fprintf(stderr, "harness: "+format+"\n", args...)
	}

	coreH := core.NewStubHandler()
	retrH := retrieval.NewStubHandler()

	scanner := bufio.NewScanner(stdin)
	scanner.Buffer(make([]byte, 0, 64*1024), scanBufferMax)
	encoder := json.NewEncoder(stdout)
	encoder.SetEscapeHTML(false)

	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var req bittensor.ChallengeRequest
		if err := json.Unmarshal(line, &req); err != nil {
			return fmt.Errorf("invalid challenge JSON: %w", err)
		}

		resp := dispatch(coreH, retrH, req, logf)
		if err := encoder.Encode(resp); err != nil {
			return fmt.Errorf("write response: %w", err)
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("read stdin: %w", err)
	}
	return nil
}

func dispatch(
	coreH *core.StubHandler,
	retrH *retrieval.StubHandler,
	req bittensor.ChallengeRequest,
	logf func(string, ...any),
) bittensor.MinerResponse {
	ctx := context.Background()
	if req.DeadlineMs > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(req.DeadlineMs)*time.Millisecond)
		defer cancel()
	}

	started := time.Now().UTC()
	var (
		resp bittensor.MinerResponse
		err  error
	)
	switch req.Mechanism {
	case bittensor.MechanismCore:
		resp, err = coreH.Handle(ctx, req)
	case bittensor.MechanismRetrieval:
		resp, err = retrH.Handle(ctx, req)
	default:
		resp = bittensor.MinerResponse{Refusal: "unknown_mechanism"}
	}
	finished := time.Now().UTC()
	if err != nil {
		logf("handler error on case %s: %v", req.CaseID, err)
		resp = bittensor.MinerResponse{Refusal: "handler_error"}
	}

	// Stamp protocol envelope fields the handlers should not have to fill in
	// themselves. Echoing schema_version, challenge_id, and validator_seed is
	// mandatory; lost or duplicated seeds are protocol violations.
	resp.SchemaVersion = bittensor.SchemaVersion
	resp.ChallengeID = req.ChallengeID
	resp.ValidatorSeed = req.ValidatorSeed
	if resp.StartedAt.IsZero() {
		resp.StartedAt = started
	}
	if resp.FinishedAt.IsZero() {
		resp.FinishedAt = finished
	}
	if resp.TotalLatencyMs == 0 {
		resp.TotalLatencyMs = resp.FinishedAt.Sub(resp.StartedAt).Milliseconds()
	}
	return resp
}
