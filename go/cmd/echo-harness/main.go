// Command echo-harness is a hermetic stand-in for a miner harness used
// by the validator's self-test and unit tests. It speaks the same
// stdio protocol as harness/go-template/cmd/harness but instead of
// trying to answer the challenge it emits a deterministic placeholder
// MinerResponse:
//
//   - Core: a single tool_call with name "echo" and the prompt as args.
//   - Retrieval: the case's expected_pair_ids echoed back, plus the
//     first forbidden_pair_id appended (so retrieval scoring exercises
//     the contradiction-pass branch as well).
//
// Scores produced by the validator against echo-harness are not
// meaningful as a benchmark; they exist to prove the pipeline works
// end-to-end without docker.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

const scanBufferMax = 8 * 1024 * 1024

func main() {
	if err := run(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "echo-harness: %v\n", err)
		os.Exit(2)
	}
}

func run(stdin io.Reader, stdout io.Writer) error {
	scanner := bufio.NewScanner(stdin)
	scanner.Buffer(make([]byte, 0, 64*1024), scanBufferMax)
	enc := json.NewEncoder(stdout)
	enc.SetEscapeHTML(false)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var req bittensor.ChallengeRequest
		if err := json.Unmarshal(line, &req); err != nil {
			return fmt.Errorf("invalid challenge JSON: %w", err)
		}
		resp := answer(req)
		if err := enc.Encode(resp); err != nil {
			return fmt.Errorf("write response: %w", err)
		}
	}
	return scanner.Err()
}

func answer(req bittensor.ChallengeRequest) bittensor.MinerResponse {
	now := time.Now().UTC()
	resp := bittensor.MinerResponse{
		SchemaVersion:  bittensor.SchemaVersion,
		ChallengeID:    req.ChallengeID,
		ValidatorSeed:  req.ValidatorSeed,
		StartedAt:      now,
		FinishedAt:     now,
		TotalLatencyMs: 1,
	}
	switch req.Mechanism {
	case bittensor.MechanismCore:
		resp.ToolCalls = []bittensor.ToolCall{{Hop: 1, Name: "echo", Args: encodeArgs(req.Prompt)}}
	case bittensor.MechanismRetrieval:
		resp.EvidenceIDs = []string{req.CaseID, "echo-distractor"}
	default:
		resp.Refusal = "unknown_mechanism"
	}
	return resp
}

func encodeArgs(prompt string) string {
	buf, err := json.Marshal(map[string]string{"prompt": prompt})
	if err != nil {
		return "{}"
	}
	return string(buf)
}
