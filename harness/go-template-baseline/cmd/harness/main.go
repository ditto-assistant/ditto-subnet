// Command harness is the reference DittoBench baseline miner.
//
// Unlike the stub at ../../go-template, this binary actually attempts
// to answer challenges: Core via an OpenAI-compatible chat completions
// endpoint, Retrieval via an in-memory BM25 index built lazily over
// the validator-mounted fixture corpus. It is meant to be a concrete
// "did I beat the baseline by X%?" anchor, not a competitive miner.
//
// Replace the handlers under internal/core and internal/retrieval to
// build something stronger.
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
	"github.com/heyditto/ditto-subnet/harness/go-template-baseline/internal/core"
	"github.com/heyditto/ditto-subnet/harness/go-template-baseline/internal/retrieval"
)

const scanBufferMax = 8 * 1024 * 1024

func main() {
	if err := run(os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "baseline-harness: %v\n", err)
		os.Exit(2)
	}
}

func run(stdin io.Reader, stdout, stderr io.Writer) error {
	logf := func(format string, args ...any) {
		fmt.Fprintf(stderr, "baseline-harness: "+format+"\n", args...)
	}

	coreH := core.NewOpenAIHandler()
	retrH := retrieval.NewBM25Handler()

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
	coreH *core.OpenAIHandler,
	retrH *retrieval.BM25Handler,
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
