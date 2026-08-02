package refharness

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// execTimeout bounds a single mock tool-execution call.
const execTimeout = 15 * time.Second

// Execute runs a routed trajectory against the validator-served mock tool
// endpoint (observed tool execution). For each call it POSTs
// a ToolExecRequest (so the validator OBSERVES the trajectory) and collects the
// result; the last non-empty result is returned so the harness can incorporate
// returned content into its answer (result-usage). endpoint=="" is a no-op (the
// harness has nothing to route through). A per-call error is returned only if the
// endpoint is unreachable; a tool the endpoint declines to serve (a memory tool)
// is simply skipped, matching how a real harness would run memory locally.
func Execute(ctx context.Context, endpoint, caseID, userID string, calls []protocol.ObservedToolCall) (string, error) {
	if endpoint == "" || len(calls) == 0 {
		return "", nil
	}
	last := ""
	for _, c := range calls {
		res, err := postToolCall(ctx, endpoint, protocol.ToolExecRequest{
			CaseID: caseID,
			UserID: userID,
			Name:   c.Name,
			Args:   c.Args,
			Hop:    c.Hop,
		})
		if err != nil {
			return last, err
		}
		if res.Result != "" {
			last = res.Result
		}
	}
	return last, nil
}

func postToolCall(ctx context.Context, endpoint string, req protocol.ToolExecRequest) (protocol.ToolExecResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, execTimeout)
	defer cancel()

	buf, err := json.Marshal(req)
	if err != nil {
		return protocol.ToolExecResponse{}, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(buf))
	if err != nil {
		return protocol.ToolExecResponse{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(httpReq)
	if err != nil {
		return protocol.ToolExecResponse{}, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return protocol.ToolExecResponse{}, err
	}
	var out protocol.ToolExecResponse
	if err := json.Unmarshal(body, &out); err != nil {
		return protocol.ToolExecResponse{}, fmt.Errorf("decode tool-exec response: %w", err)
	}
	return out, nil
}
