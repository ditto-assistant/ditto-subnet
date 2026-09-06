package codinghostedrelay

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime"
	"net/http"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/google/uuid"
)

// Config is trusted worker configuration. The source must already be registered
// from the exact screened container's observed address, never from miner input.
type Config struct {
	Router                            *codingsource.Router
	Source                            codingsource.HostedBinding
	GrantID, PolicySHA256, SocketPath string
	Token                             []byte
}

func (Config) String() string               { return "HostedRelayConfig{private}" }
func (Config) GoString() string             { return "HostedRelayConfig{private}" }
func (Config) MarshalJSON() ([]byte, error) { return nil, ErrRelay }

type Relay struct {
	mu            sync.Mutex
	backend       *client
	route         codingcertifier.PublishedCapability
	deadline      time.Time
	busy, revoked bool
	cancel        context.CancelFunc
}

// Publish may return a non-nil cleanup handle with an error after private bridge
// activity. Retain and revoke it; never retry publication after an ambiguous open.
func Publish(ctx context.Context, config Config) (*Relay, error) {
	if ctx == nil || ctx.Err() != nil || config.Router == nil || !config.Source.Deadline.After(time.Now()) {
		return nil, ErrRelay
	}
	backend, err := newClient(config.SocketPath, config.Token, config.Source, config.GrantID, config.PolicySHA256)
	if err != nil {
		return nil, err
	}
	relay := &Relay{backend: backend, deadline: time.Unix(config.Source.Deadline.Unix(), 0)}
	opened, err := backend.call(ctx, "open", uuid.NewString(), nil)
	if err != nil || opened.LedgerDrained != nil {
		relay.revoked = true
		return relay, ErrRelay
	}
	relay.route, err = config.Router.PublishHostedInference(ctx, config.Source, http.HandlerFunc(relay.serveHTTP))
	if err != nil {
		cleanup, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		relay.revoked = true
		_ = relay.Revoke(cleanup)
		return relay, ErrRelay
	}
	return relay, nil
}

func (r *Relay) URL() string {
	if r == nil {
		return ""
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.revoked || r.route == nil {
		return ""
	}
	return r.route.URL()
}

// Revoke closes local admission and cancels transport, drains the source route,
// then asks the private bridge to revoke/drain its provider task and ledger.
// ErrUnsettled is not permission to freeze or restart. Retain this handle for
// cleanup retries. Local socket EOF alone never proves remote provider shutdown.
func (r *Relay) Revoke(ctx context.Context) error {
	if r == nil || ctx == nil {
		return ErrRelay
	}
	r.mu.Lock()
	r.revoked = true
	if r.cancel != nil {
		r.cancel()
	}
	r.mu.Unlock()
	if r.route != nil {
		if err := r.route.Revoke(ctx); err != nil {
			return err
		}
	}
	reply, err := r.backend.call(ctx, "revoke", uuid.NewString(), nil)
	if err != nil {
		return err
	}
	if !*reply.LedgerDrained {
		return ErrUnsettled
	}
	if r.route != nil {
		return r.route.Close()
	}
	return nil
}

func (r *Relay) serveHTTP(w http.ResponseWriter, request *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Type", "application/json")
	if request.Method != http.MethodPost || request.URL.Path != "/chat/completions" || request.URL.RawPath != "" || request.URL.RawQuery != "" {
		reject(w, http.StatusNotFound)
		return
	}
	media, _, err := mime.ParseMediaType(request.Header.Get("Content-Type"))
	if err != nil || media != "application/json" || request.Header.Get("Content-Encoding") != "" {
		reject(w, http.StatusUnsupportedMediaType)
		return
	}
	r.mu.Lock()
	if r.revoked || r.busy || !r.deadline.After(time.Now()) {
		r.mu.Unlock()
		reject(w, http.StatusGone)
		return
	}
	ctx, cancel := context.WithDeadline(request.Context(), r.deadline)
	r.busy = true
	r.cancel = cancel
	r.mu.Unlock()
	defer func() { cancel(); r.mu.Lock(); r.busy = false; r.cancel = nil; r.mu.Unlock() }()
	// The real router response supports read/write deadlines. Closing a body on
	// cancellation also bounds non-network readers used by trusted callers/tests.
	stop := context.AfterFunc(ctx, func() { _ = request.Body.Close() })
	defer stop()
	_ = http.NewResponseController(w).SetReadDeadline(minTime(r.deadline, time.Now().Add(10*time.Second)))
	body, err := io.ReadAll(http.MaxBytesReader(w, request.Body, maxRequest))
	if err != nil || codingcontract.ValidateJSONDocument(body, maxRequest) != nil {
		reject(w, http.StatusBadRequest)
		return
	}
	requestID := uuid.NewString()
	reply, err := r.backend.call(ctx, "complete", requestID, body)
	if err != nil {
		r.fail()
		reject(w, http.StatusBadGateway)
		return
	}
	output, err := project(reply, r.backend.binding, requestID)
	if err != nil {
		r.fail()
		reject(w, http.StatusBadGateway)
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.revoked || ctx.Err() != nil || !r.deadline.After(time.Now()) {
		reject(w, http.StatusGone)
		return
	}
	_ = http.NewResponseController(w).SetWriteDeadline(minTime(r.deadline, time.Now().Add(5*time.Second)))
	_, _ = w.Write(output)
}

func (r *Relay) fail() { r.mu.Lock(); r.revoked = true; r.mu.Unlock() }
func reject(w http.ResponseWriter, status int) {
	w.WriteHeader(status)
	_, _ = w.Write([]byte("{\"error\":{\"code\":\"hosted_relay_unavailable\"}}\n"))
}
func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}
func (*Relay) String() string               { return "HostedInferenceRelay{private}" }
func (*Relay) GoString() string             { return "HostedInferenceRelay{private}" }
func (*Relay) MarshalJSON() ([]byte, error) { return nil, ErrRelay }

func project(reply result, b binding, requestID string) ([]byte, error) {
	if len(reply.Response) == 0 || len(reply.Response) > 8<<20 || reply.LedgerDrained != nil || codingcontract.ValidateJSONDocument(reply.Response, 8<<20) != nil {
		return nil, ErrRelay
	}
	var normalized codingcontract.InferenceNormalizedResponse
	var projection map[string]json.RawMessage
	if json.Unmarshal(reply.Response, &projection) != nil || !required(projection, "schema", "id", "model", "provider", "choices", "usage") {
		return nil, ErrRelay
	}
	var usageFields map[string]json.RawMessage
	if json.Unmarshal(projection["usage"], &usageFields) != nil || !required(usageFields, "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd_micros") {
		return nil, ErrRelay
	}
	if json.Unmarshal(reply.Response, &normalized) != nil || normalized.Schema != "dittobench-coding-hosted-inference-response-v2" || normalized.Model != "openai/gpt-5.6-luna" || normalized.Provider != "Azure" || normalized.ID == "" || len(normalized.ID) > 256 || len(normalized.Choices) != 1 || normalized.Choices[0].Validate() != nil {
		return nil, ErrRelay
	}
	var receipt struct {
		Schema                string `json:"schema"`
		EvaluationID          string `json:"evaluation_id"`
		AttemptID             string `json:"attempt_id"`
		GrantID               string `json:"grant_id"`
		RequestID             string `json:"request_id"`
		PolicySHA256          string `json:"policy_sha256"`
		ResponseSHA256        string `json:"response_sha256"`
		PromptTokens          uint64 `json:"prompt_tokens"`
		CompletionTokens      uint64 `json:"completion_tokens"`
		Cost                  uint64 `json:"cost_usd_micros"`
		Sequence              uint32 `json:"sequence"`
		LockedRequestSHA256   string `json:"locked_request_sha256"`
		ProviderReceiptSHA256 string `json:"provider_receipt_sha256"`
		Model                 string `json:"model"`
		Provider              string `json:"provider"`
		Route                 string `json:"provider_route"`
		RouteProfile          string `json:"provider_route_profile"`
		Fallback              *bool  `json:"fallback_used"`
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(reply.Settlement, &fields) != nil || !required(fields, "prompt_tokens", "completion_tokens", "cost_usd_micros") {
		return nil, ErrRelay
	}
	if json.Unmarshal(reply.Settlement, &receipt) != nil {
		return nil, ErrRelay
	}
	sum := sha256.Sum256(reply.Response)
	usage := normalized.Usage
	if receipt.Sequence == 0 || receipt.Sequence > 256 || !lowerDigest(receipt.LockedRequestSHA256) || !lowerDigest(receipt.ProviderReceiptSHA256) || receipt.Model != "openai/gpt-5.6-luna" || receipt.Provider != "Azure" || receipt.Route != "azure/eu" || receipt.RouteProfile != "luna-azure-eu-zdr-v1" || receipt.Fallback == nil || *receipt.Fallback {
		return nil, ErrRelay
	}
	if receipt.Schema != "dittobench-coding-hosted-inference-settlement-v2" || receipt.EvaluationID != b.EvaluationID || receipt.AttemptID != b.AttemptID || receipt.GrantID != b.GrantID || receipt.RequestID != requestID || receipt.PolicySHA256 != b.PolicySHA256 || receipt.ResponseSHA256 != hex.EncodeToString(sum[:]) || receipt.PromptTokens != usage.PromptTokens || receipt.CompletionTokens != usage.CompletionTokens || receipt.Cost != usage.CostUSDMicros || usage.PromptTokens > 2_250_000 || usage.CompletionTokens > 32768 || usage.TotalTokens != usage.PromptTokens+usage.CompletionTokens {
		return nil, ErrRelay
	}
	return canonical(codingcontract.InferenceMinerResponse{ID: normalized.ID, Model: normalized.Model, Choices: normalized.Choices, Usage: codingcontract.InferenceMinerUsage{PromptTokens: usage.PromptTokens, CompletionTokens: usage.CompletionTokens, TotalTokens: usage.TotalTokens}})
}

func required(fields map[string]json.RawMessage, names ...string) bool {
	for _, name := range names {
		value, ok := fields[name]
		if !ok || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return false
		}
	}
	return true
}
func lowerDigest(value string) bool {
	raw, err := hex.DecodeString(value)
	return err == nil && len(raw) == 32 && hex.EncodeToString(raw) == value
}
