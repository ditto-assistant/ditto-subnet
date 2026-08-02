package providercert

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"
)

type Client struct {
	APIURL      string
	APIKey      string
	HTTPClient  *http.Client
	MaxAttempts int
}

type RunOptions struct {
	Model     string
	Runs      int
	RunOffset int
	Providers map[string]bool
}

type chatResponse struct {
	ID       string `json:"id"`
	Model    string `json:"model"`
	Provider string `json:"provider"`
	Choices  []struct {
		FinishReason       string `json:"finish_reason"`
		NativeFinishReason string `json:"native_finish_reason"`
		Message            struct {
			Role             string            `json:"role"`
			Content          string            `json:"content"`
			Reasoning        string            `json:"reasoning"`
			ReasoningDetails []json.RawMessage `json:"reasoning_details"`
			ToolCalls        []struct {
				Index    int    `json:"index"`
				ID       string `json:"id"`
				Type     string `json:"type"`
				Function struct {
					Name      string `json:"name"`
					Arguments string `json:"arguments"`
				} `json:"function"`
			} `json:"tool_calls"`
		} `json:"message"`
		Delta struct {
			Content          string            `json:"content"`
			Reasoning        string            `json:"reasoning"`
			ReasoningDetails []json.RawMessage `json:"reasoning_details"`
			ToolCalls        []struct {
				Index    int    `json:"index"`
				ID       string `json:"id"`
				Function struct {
					Name      string `json:"name"`
					Arguments string `json:"arguments"`
				} `json:"function"`
			} `json:"tool_calls"`
		} `json:"delta"`
	} `json:"choices"`
	Usage struct {
		PromptTokens      int64   `json:"prompt_tokens"`
		CompletionTokens  int64   `json:"completion_tokens"`
		TotalTokens       int64   `json:"total_tokens"`
		Cost              float64 `json:"cost"`
		CompletionDetails struct {
			ReasoningTokens int64 `json:"reasoning_tokens"`
		} `json:"completion_tokens_details"`
	} `json:"usage"`
	Error *struct {
		Code     int            `json:"code"`
		Message  string         `json:"message"`
		Metadata map[string]any `json:"metadata"`
	} `json:"error"`
}

func (c *Client) FetchInventory(ctx context.Context, model string) (Inventory, error) {
	if model == "" {
		model = DefaultModel
	}
	providers, err := c.fetchProviderSlugs(ctx)
	if err != nil {
		return Inventory{}, err
	}
	url := strings.TrimRight(c.apiURL(), "/") + "/models/" + model + "/endpoints"
	var wire struct {
		Data struct {
			ID           string     `json:"id"`
			Name         string     `json:"name"`
			Architecture any        `json:"architecture"`
			Endpoints    []Endpoint `json:"endpoints"`
		} `json:"data"`
	}
	if err := c.getJSON(ctx, url, &wire); err != nil {
		return Inventory{}, err
	}
	for i := range wire.Data.Endpoints {
		wire.Data.Endpoints[i].ProviderSlug = providers[wire.Data.Endpoints[i].ProviderName]
	}
	return Inventory{
		CapturedAt: time.Now().UTC(), Model: wire.Data.ID, ModelName: wire.Data.Name,
		Architecture: wire.Data.Architecture, Endpoints: wire.Data.Endpoints,
	}, nil
}

func (c *Client) Run(ctx context.Context, opts RunOptions) (Report, error) {
	if strings.TrimSpace(c.APIKey) == "" {
		return Report{}, errors.New("OPENROUTER_API_KEY is required")
	}
	if opts.Model == "" {
		opts.Model = DefaultModel
	}
	if opts.Runs < 1 {
		opts.Runs = 1
	}
	started := time.Now().UTC()
	inv, err := c.FetchInventory(ctx, opts.Model)
	if err != nil {
		return Report{}, err
	}
	report := Report{
		SchemaVersion: SchemaVersion, StartedAt: started, Model: opts.Model,
		RunsPerScenario: opts.Runs, RunOffset: opts.RunOffset, Inventory: inv,
		Routing: map[string]any{
			"only": "single provider slug", "order": "single provider slug",
			"allow_fallbacks": false, "require_parameters": false,
			"reasoning_enabled": false,
		},
	}
	for _, ep := range inv.Endpoints {
		if len(opts.Providers) > 0 && !opts.Providers[ep.ProviderSlug] {
			continue
		}
		for run := opts.RunOffset + 1; run <= opts.RunOffset+opts.Runs; run++ {
			for _, sc := range scenarios() {
				result := c.runScenario(ctx, opts.Model, ep, sc, run)
				report.Results = append(report.Results, result)
			}
		}
	}
	report.FinishedAt = time.Now().UTC()
	report.Summary = Summarize(report.Results)
	return report, nil
}

func (c *Client) runScenario(ctx context.Context, model string, ep Endpoint, sc scenario, run int) Result {
	result := Result{Provider: ep.ProviderName, ProviderSlug: ep.ProviderSlug, EndpointTag: ep.Tag, Scenario: sc.Name, Run: run, Streaming: sc.Streaming}
	body := requestBody(model, ep.ProviderSlug, sc)
	first, status, attempts, latency, ttft, err := c.chat(ctx, body, sc.Streaming)
	result.HTTPStatus, result.Attempts, result.LatencyMS, result.TTFTMS = status, attempts, latency.Milliseconds(), ttft.Milliseconds()
	if err != nil {
		result.Errors = append(result.Errors, err.Error())
		return result
	}
	result.absorb(first)
	if sc.Name == "continuation" || sc.Name == "tool_error_continuation" {
		if len(result.ToolCalls) == 0 {
			result.Errors = append(result.Errors, "initial response contained no tool call")
			return result
		}
		messages := append([]map[string]any(nil), sc.Messages...)
		messages = append(messages, assistantToolMessage(first), map[string]any{
			"role": "tool", "tool_call_id": result.ToolCalls[0].ID,
			"content": continuationContent(sc.Name),
		})
		body["messages"] = messages
		body["tool_choice"] = "none"
		second, status2, attempts2, latency2, _, err2 := c.chat(ctx, body, false)
		result.Attempts += attempts2
		result.LatencyMS += latency2.Milliseconds()
		result.HTTPStatus = status2
		if err2 != nil {
			result.Errors = append(result.Errors, "continuation: "+err2.Error())
			return result
		}
		result.Usage.PromptTokens += second.Usage.PromptTokens
		result.Usage.CompletionTokens += second.Usage.CompletionTokens
		result.Usage.TotalTokens += second.Usage.TotalTokens
		result.Usage.ReasoningTokens += second.Usage.CompletionDetails.ReasoningTokens
		result.Usage.Cost += second.Usage.Cost
		if len(second.Choices) == 0 {
			result.Errors = append(result.Errors, "continuation response contained no choice")
			return result
		}
		content := second.Choices[0].Message.Content
		result.ContentLength += len(content)
		result.FinishReason = second.Choices[0].FinishReason
		result.NativeFinishReason = second.Choices[0].NativeFinishReason
		want := "CERT_RECORD_READY"
		if sc.Name == "tool_error_continuation" {
			want = "CERT_TOOL_ERROR_HANDLED"
		}
		if !strings.Contains(content, want) {
			result.Errors = append(result.Errors, "continuation did not incorporate expected tool result")
		}
	}
	result.Errors = append(result.Errors, validate(sc.Name, model, result)...)
	result.Success = len(result.Errors) == 0
	return result
}

func requestBody(model, provider string, sc scenario) map[string]any {
	body := map[string]any{
		"model": model, "messages": sc.Messages, "stream": sc.Streaming,
		"max_tokens": 96, "temperature": 0, "seed": 118,
		"reasoning":            map[string]any{"enabled": false, "exclude": false},
		"chat_template_kwargs": map[string]any{"enable_thinking": false},
		"provider": map[string]any{
			"only": []string{provider}, "order": []string{provider},
			"allow_fallbacks": false, "require_parameters": false,
		},
	}
	if sc.Streaming {
		body["stream_options"] = map[string]any{"include_usage": true}
	}
	if len(sc.Tools) > 0 {
		body["tools"] = sc.Tools
	}
	if sc.ToolChoice != nil {
		body["tool_choice"] = sc.ToolChoice
	}
	if sc.Parallel != nil {
		body["parallel_tool_calls"] = *sc.Parallel
	}
	return body
}

func (c *Client) chat(ctx context.Context, body map[string]any, stream bool) (chatResponse, int, int, time.Duration, time.Duration, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return chatResponse{}, 0, 0, 0, 0, err
	}
	max := c.MaxAttempts
	if max < 1 {
		max = 3
	}
	var last error
	var status int
	var total time.Duration
	for attempt := 1; attempt <= max; attempt++ {
		start := time.Now()
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(c.apiURL(), "/")+"/chat/completions", bytes.NewReader(raw))
		if err != nil {
			return chatResponse{}, 0, attempt, total, 0, err
		}
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
		req.Header.Set("Content-Type", "application/json")
		SetAttributionHeaders(req.Header)
		resp, err := c.httpClient().Do(req)
		total += time.Since(start)
		if err != nil {
			last = err
		} else {
			status = resp.StatusCode
			out, ttft, decodeErr := decodeResponse(resp, stream, start)
			_ = resp.Body.Close()
			if decodeErr == nil && status >= 200 && status < 300 && out.Error == nil {
				return out, status, attempt, total, ttft, nil
			}
			last = decodeErr
			if out.Error != nil {
				last = fmt.Errorf("openrouter error %d: %s", out.Error.Code, out.Error.Message)
			}
			if last == nil {
				last = fmt.Errorf("openrouter returned HTTP %d", status)
			}
			if status != http.StatusTooManyRequests && status < 500 {
				return chatResponse{}, status, attempt, total, ttft, last
			}
		}
		if attempt < max {
			select {
			case <-ctx.Done():
				return chatResponse{}, status, attempt, total, 0, ctx.Err()
			case <-time.After(time.Duration(1<<(attempt-1)) * 250 * time.Millisecond):
			}
		}
	}
	return chatResponse{}, status, max, total, 0, last
}

func decodeResponse(resp *http.Response, stream bool, started time.Time) (chatResponse, time.Duration, error) {
	if !stream {
		var out chatResponse
		if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&out); err != nil {
			return out, 0, err
		}
		return out, 0, nil
	}
	return decodeStream(resp.Body, started)
}

func decodeStream(r io.Reader, started time.Time) (chatResponse, time.Duration, error) {
	var out chatResponse
	var ttft time.Duration
	toolIDs := map[int]string{}
	toolNames := map[int]string{}
	toolArgs := map[int]string{}
	scanner := bufio.NewScanner(io.LimitReader(r, 8<<20))
	scanner.Buffer(make([]byte, 64<<10), 2<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		payload := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if payload == "[DONE]" {
			break
		}
		var chunk chatResponse
		if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
			return out, ttft, fmt.Errorf("decode SSE chunk: %w", err)
		}
		if ttft == 0 && len(chunk.Choices) > 0 {
			ttft = time.Since(started)
		}
		if out.ID == "" {
			out.ID, out.Model, out.Provider = chunk.ID, chunk.Model, chunk.Provider
		}
		out.Usage = chunk.Usage
		if len(chunk.Choices) == 0 {
			continue
		}
		if len(out.Choices) == 0 {
			out.Choices = make([]struct {
				FinishReason       string `json:"finish_reason"`
				NativeFinishReason string `json:"native_finish_reason"`
				Message            struct {
					Role             string            `json:"role"`
					Content          string            `json:"content"`
					Reasoning        string            `json:"reasoning"`
					ReasoningDetails []json.RawMessage `json:"reasoning_details"`
					ToolCalls        []struct {
						Index    int    `json:"index"`
						ID       string `json:"id"`
						Type     string `json:"type"`
						Function struct {
							Name      string `json:"name"`
							Arguments string `json:"arguments"`
						} `json:"function"`
					} `json:"tool_calls"`
				} `json:"message"`
				Delta struct {
					Content          string            `json:"content"`
					Reasoning        string            `json:"reasoning"`
					ReasoningDetails []json.RawMessage `json:"reasoning_details"`
					ToolCalls        []struct {
						Index    int    `json:"index"`
						ID       string `json:"id"`
						Function struct {
							Name      string `json:"name"`
							Arguments string `json:"arguments"`
						} `json:"function"`
					} `json:"tool_calls"`
				} `json:"delta"`
			}, 1)
		}
		choice := chunk.Choices[0]
		out.Choices[0].FinishReason = choice.FinishReason
		out.Choices[0].NativeFinishReason = choice.NativeFinishReason
		out.Choices[0].Message.Content += choice.Delta.Content
		out.Choices[0].Message.Reasoning += choice.Delta.Reasoning
		out.Choices[0].Message.ReasoningDetails = append(out.Choices[0].Message.ReasoningDetails, choice.Delta.ReasoningDetails...)
		for _, call := range choice.Delta.ToolCalls {
			toolIDs[call.Index] += call.ID
			toolNames[call.Index] += call.Function.Name
			toolArgs[call.Index] += call.Function.Arguments
		}
	}
	if err := scanner.Err(); err != nil {
		return out, ttft, err
	}
	indexes := make([]int, 0, len(toolIDs))
	for i := range toolIDs {
		indexes = append(indexes, i)
	}
	for i := range toolNames {
		if _, ok := toolIDs[i]; !ok {
			indexes = append(indexes, i)
		}
	}
	sort.Ints(indexes)
	seen := map[int]bool{}
	for _, i := range indexes {
		if seen[i] {
			continue
		}
		seen[i] = true
		var call struct {
			Index    int    `json:"index"`
			ID       string `json:"id"`
			Type     string `json:"type"`
			Function struct {
				Name      string `json:"name"`
				Arguments string `json:"arguments"`
			} `json:"function"`
		}
		call.Index, call.ID, call.Type = i, toolIDs[i], "function"
		call.Function.Name, call.Function.Arguments = toolNames[i], toolArgs[i]
		out.Choices[0].Message.ToolCalls = append(out.Choices[0].Message.ToolCalls, call)
	}
	return out, ttft, nil
}

func (r *Result) absorb(resp chatResponse) {
	r.Model, r.ResponseProvider = resp.Model, resp.Provider
	r.Usage = Usage{PromptTokens: resp.Usage.PromptTokens, CompletionTokens: resp.Usage.CompletionTokens, TotalTokens: resp.Usage.TotalTokens, ReasoningTokens: resp.Usage.CompletionDetails.ReasoningTokens, Cost: resp.Usage.Cost}
	if len(resp.Choices) == 0 {
		r.Errors = append(r.Errors, "response contained no choices")
		return
	}
	choice := resp.Choices[0]
	r.FinishReason, r.NativeFinishReason = choice.FinishReason, choice.NativeFinishReason
	r.content = choice.Message.Content
	r.ContentLength = len(choice.Message.Content)
	r.ReasoningPresent = choice.Message.Reasoning != ""
	r.ReasoningDetails = len(choice.Message.ReasoningDetails)
	for _, call := range choice.Message.ToolCalls {
		r.ToolCalls = append(r.ToolCalls, ToolCall{ID: call.ID, Name: call.Function.Name, Arguments: rawObject(call.Function.Arguments)})
	}
}

func validate(name, model string, r Result) []string {
	var errs []string
	if r.Model != model {
		errs = append(errs, "response model did not match requested model")
	}
	if r.ResponseProvider != "" && !strings.EqualFold(r.ResponseProvider, r.Provider) {
		errs = append(errs, "response provider did not match explicit provider pin")
	}
	if r.Usage.TotalTokens == 0 {
		errs = append(errs, "usage.total_tokens was missing or zero")
	}
	if r.ReasoningPresent || r.Usage.ReasoningTokens > 0 {
		errs = append(errs, "reasoning was present despite reasoning being disabled")
	}
	if name == "text" {
		if r.FinishReason != "stop" || strings.TrimSpace(r.content) != "CERT_TEXT_OK" {
			errs = append(errs, "text response lacked content or stop finish reason")
		}
		return errs
	}
	want := 1
	if name == "parallel_tools" {
		want = 2
	}
	if len(r.ToolCalls) < want {
		errs = append(errs, fmt.Sprintf("got %d tool calls, want at least %d", len(r.ToolCalls), want))
	}
	for _, call := range r.ToolCalls {
		if call.ID == "" || call.Name == "" {
			errs = append(errs, "tool call id or name was empty")
		}
		var args map[string]any
		if len(call.Arguments) == 0 || json.Unmarshal(call.Arguments, &args) != nil {
			errs = append(errs, "tool call arguments were not a JSON object")
		}
	}
	if name == "parallel_tools" {
		names := map[string]bool{}
		for _, call := range r.ToolCalls {
			names[call.Name] = true
		}
		if !names["weather_paris"] || !names["weather_tokyo"] {
			errs = append(errs, "parallel response did not contain both expected tool names")
		}
	} else if len(r.ToolCalls) > 0 && r.ToolCalls[0].Name != "lookup_record" {
		errs = append(errs, "tool response used the wrong function name")
	}
	return errs
}

func assistantToolMessage(resp chatResponse) map[string]any {
	choice := resp.Choices[0]
	return map[string]any{"role": "assistant", "content": choice.Message.Content, "tool_calls": choice.Message.ToolCalls}
}

func continuationContent(name string) string {
	if name == "tool_error_continuation" {
		return `{"error":"record backend unavailable"}`
	}
	return `{"record_id":"record-17","status":"CERT_RECORD_READY"}`
}

func (c *Client) fetchProviderSlugs(ctx context.Context) (map[string]string, error) {
	var wire struct {
		Data []struct{ Name, Slug string } `json:"data"`
	}
	if err := c.getJSON(ctx, strings.TrimRight(c.apiURL(), "/")+"/providers", &wire); err != nil {
		return nil, err
	}
	out := make(map[string]string, len(wire.Data))
	for _, p := range wire.Data {
		out[p.Name] = p.Slug
	}
	return out, nil
}

func (c *Client) getJSON(ctx context.Context, url string, dst any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	SetAttributionHeaders(req.Header)
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("GET %s returned %d", url, resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(dst)
}

// SetAttributionHeaders identifies Ditto on every OpenRouter request.
func SetAttributionHeaders(header http.Header) {
	header.Set("HTTP-Referer", AppReferer)
	header.Set("X-OpenRouter-Title", AppTitle)
	header.Set("X-Title", AppTitle)
}

func (c *Client) apiURL() string {
	if c.APIURL != "" {
		return c.APIURL
	}
	return DefaultAPIURL
}

func (c *Client) httpClient() *http.Client {
	if c.HTTPClient != nil {
		return c.HTTPClient
	}
	return &http.Client{Timeout: 90 * time.Second}
}
