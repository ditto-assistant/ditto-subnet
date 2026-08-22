// Package traces captures every hosted-inference call the relay brokers —
// the miner's request as received, the payload actually sent upstream, every
// provider attempt's raw answer, the sanitized response returned, usage,
// timing, and the grant context — and ships it, durably and off the request
// path, to one or more S3-compatible buckets (Hippius primary, Backblaze B2
// mirror, or any other endpoint).
//
// Why this exists: the Postgres ledger (inference_requests) is metadata only
// and load-bearing for admission, replay protection and accounting, so it is
// short-retention by design. The bodies — the DittoBench training data — were
// never persisted anywhere. This package is that persistence.
//
// Shape: a bounded in-process queue feeds ONE writer goroutine that appends
// JSONL to per-stream spool files on local disk, rotating by size and age.
// An uploader goroutine zstd-compresses rotated files and PUTs them to every
// configured sink, remembering per-sink completion in a sidecar so a sink
// that is down does not block the others and a restart resumes exactly where
// it stopped. A file leaves the disk only when every REQUIRED sink holds it.
// Nothing here ever blocks an inference request: when the queue or the disk
// budget is full the record is dropped and counted, never waited on.
package traces

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"
)

// SchemaVersion names the record layout. Bump it when a field changes meaning;
// adding fields is backward compatible and does not bump it.
const SchemaVersion = "ditto.inference.trace.v1"

// Event values.
const (
	EventSettled  = "inference.settled"  // an admitted call that reached settlement
	EventDeclined = "inference.declined" // an authenticated call the admission gate refused
	EventBackfill = "ledger.backfill"    // a historical inference_requests row (no bodies)
)

// Lane values (mirrors the relay's endpoint families, not the decline lane vocabulary).
const (
	LaneInference    = "inference"
	LaneConfirmation = "confirmation"
)

// Kind values.
const (
	KindChat      = "chat"
	KindEmbedding = "embedding"
)

// Record is one captured call. Fields are JSON with stable snake_case names;
// raw JSON bodies are embedded verbatim (json.RawMessage) so nothing the
// provider or the miner sent is re-interpreted on the way to storage.
type Record struct {
	Schema     string     `json:"schema"`
	Event      string     `json:"event"`
	RecordedAt time.Time  `json:"recorded_at"`
	Relay      Relay      `json:"relay"`
	Request    Request    `json:"request"`
	Grant      *Grant     `json:"grant,omitempty"`
	Admission  *Admission `json:"admission,omitempty"`
	Upstream   *Upstream  `json:"upstream,omitempty"`
	Response   *Response  `json:"response,omitempty"`
	Usage      *Usage     `json:"usage,omitempty"`
	Outcome    *Outcome   `json:"outcome,omitempty"`
}

// Relay identifies the process that produced the record.
type Relay struct {
	Instance string `json:"instance"`         // host:port of the relay slot, or "backfill"
	Commit   string `json:"commit,omitempty"` // source commit the relay reports on /health
	Source   string `json:"source"`           // "relay" | "postgres-backfill"
}

// Request is the miner-side call as the relay received it.
type Request struct {
	RequestID   string          `json:"request_id,omitempty"`
	Lane        string          `json:"lane"`
	Kind        string          `json:"kind"`
	GrantID     string          `json:"grant_id"`
	Nonce       string          `json:"nonce"`
	Generation  int64           `json:"generation"`
	RequestedAt *time.Time      `json:"requested_at,omitempty"` // X-Ditto-Requested-At
	ReceivedAt  *time.Time      `json:"received_at,omitempty"`
	Body        json.RawMessage `json:"body,omitempty"` // exact bytes the miner sent (JSON)
	BodyBytes   int64           `json:"body_bytes"`
	BodySHA256  string          `json:"body_sha256,omitempty"`
}

// Grant is the capability the call ran under, snapshotted at admission.
type Grant struct {
	AgentID         string          `json:"agent_id,omitempty"`
	BenchVersion    int32           `json:"bench_version,omitempty"`
	ValidatorHotkey string          `json:"validator_hotkey"`
	SlotID          string          `json:"slot_id,omitempty"`
	TicketDeadline  *time.Time      `json:"ticket_deadline,omitempty"`
	Status          string          `json:"status"`
	Generation      int32           `json:"generation"`
	Model           string          `json:"model,omitempty"` // the model the grant locked this call to
	AllowedModels   json.RawMessage `json:"allowed_models,omitempty"`
	RouteProvider   string          `json:"route_provider,omitempty"`
	RouteProfile    string          `json:"route_profile,omitempty"`
	RouteQuant      string          `json:"route_quantization,omitempty"`
	RequestCount    int32           `json:"request_count"` // calls booked on the grant before this one
	ExpiresAt       *time.Time      `json:"expires_at,omitempty"`
	// Confirmation-lane grants carry their purpose binding.
	TicketID        string `json:"ticket_id,omitempty"`
	BundleID        string `json:"bundle_id,omitempty"`
	Lane            string `json:"lane,omitempty"`
	Provider        string `json:"provider,omitempty"`
	ReceiptProvider string `json:"receipt_provider,omitempty"`
	ProfileRevision string `json:"profile_revision,omitempty"`
}

// Admission is what the gate decided.
type Admission struct {
	ReservedTokens      int64      `json:"reserved_tokens,omitempty"`
	MaxChargeableTokens int64      `json:"max_chargeable_tokens,omitempty"`
	AdmittedAt          *time.Time `json:"admitted_at,omitempty"`
	Decline             string     `json:"decline,omitempty"` // decline code when Event == EventDeclined
}

// Upstream is everything that happened between the relay and the provider.
type Upstream struct {
	Payload            json.RawMessage `json:"payload,omitempty"` // the locked payload sent on the final phase
	Provider           string          `json:"provider,omitempty"`
	Model              string          `json:"model,omitempty"`
	Attempts           int             `json:"attempts"`
	OpenRouterAttempts int             `json:"openrouter_attempts,omitempty"`
	FallbackPhase      int             `json:"fallback_phase"`
	TimedOut           bool            `json:"timed_out"`
	TerminalErrorCode  string          `json:"terminal_error_code,omitempty"`
	StartedAt          *time.Time      `json:"started_at,omitempty"`
	FinishedAt         *time.Time      `json:"finished_at,omitempty"`
	LatencyMs          int64           `json:"latency_ms"`
	Phases             []Phase         `json:"phases,omitempty"`
}

// Phase is one provider-route phase: the payload it sent, the raw body and
// headers it got back, and how it was classified. Retries inside a phase are
// collapsed into Attempts; the body is the last attempt's.
type Phase struct {
	Phase     int               `json:"phase"`
	Route     string            `json:"route,omitempty"` // "openrouter" | "direct" | "reliable"
	Payload   json.RawMessage   `json:"payload,omitempty"`
	Status    int               `json:"status,omitempty"`
	Headers   map[string]string `json:"headers,omitempty"`
	Body      json.RawMessage   `json:"body,omitempty"` // raw provider body, pre-sanitization
	BodyBytes int64             `json:"body_bytes,omitempty"`
	Attempts  int               `json:"attempts"`
	ErrorCode string            `json:"error_code,omitempty"`
	TimedOut  bool              `json:"timed_out,omitempty"`
}

// Response is what the relay returned to the miner.
type Response struct {
	HTTPStatus  int             `json:"http_status"`
	Body        json.RawMessage `json:"body,omitempty"` // the sanitized body, exact wire bytes
	Deliverable bool            `json:"deliverable"`
}

// Usage is the receipted accounting.
type Usage struct {
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
	CostMicrousd     int64 `json:"cost_microusd"`
	UsageAvailable   bool  `json:"usage_available"`
}

// Outcome is the ledger-side result.
type Outcome struct {
	Status      string     `json:"status"` // completed | failed | canceled | started
	StartedAt   *time.Time `json:"started_at,omitempty"`
	CompletedAt *time.Time `json:"completed_at,omitempty"`
}

// PartitionTime is the time the record files under: when it happened (the
// ledger start for a backfilled row) rather than when it was written, so a
// historical export lands in its own day/hour partitions.
func (r *Record) PartitionTime() time.Time {
	if r.Event == EventBackfill && r.Outcome != nil && r.Outcome.StartedAt != nil && !r.Outcome.StartedAt.IsZero() {
		return r.Outcome.StartedAt.UTC()
	}
	return r.RecordedAt.UTC()
}

// StreamKey is the spool/object partition a record belongs to.
func (r *Record) StreamKey() string {
	lane := r.Request.Lane
	if lane == "" {
		lane = "unknown"
	}
	kind := r.Request.Kind
	if kind == "" {
		kind = "unknown"
	}
	return lane + "-" + kind
}

// SHA256Hex is the hex digest helper used for body fingerprints.
func SHA256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// RawJSON returns b as a RawMessage when it is valid JSON, otherwise a JSON
// string carrying the bytes verbatim, so a malformed provider body is still
// stored rather than dropped.
func RawJSON(b []byte) json.RawMessage {
	if len(b) == 0 {
		return nil
	}
	if json.Valid(b) {
		return json.RawMessage(b)
	}
	quoted, err := json.Marshal(string(b))
	if err != nil {
		return nil
	}
	return json.RawMessage(quoted)
}

// TimePtr returns a pointer to t, or nil for the zero time.
func TimePtr(t time.Time) *time.Time {
	if t.IsZero() {
		return nil
	}
	u := t.UTC()
	return &u
}
