package codingrelay

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strconv"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

const (
	maximumBindingLifetime  = 2 * time.Hour
	maximumOperationTimeout = 30 * time.Second
)

var (
	ErrInvalidConfig       = errors.New("coding relay configuration is invalid")
	ErrInvalidRequest      = errors.New("coding relay request is invalid")
	ErrCapabilityRevoked   = errors.New("coding relay capability is revoked")
	ErrCapabilityExpired   = errors.New("coding relay capability is expired")
	ErrConcurrentRequest   = errors.New("coding relay request is already in flight")
	ErrBudgetExhausted     = errors.New("coding relay budget is exhausted")
	ErrProviderFailure     = errors.New("coding relay provider failed")
	ErrUpstreamUnsettled   = errors.New("coding relay upstream attempt is unsettled")
	ErrJournalUnavailable  = errors.New("coding relay journal is unavailable")
	ErrAmbiguousDispatch   = errors.New("coding relay journal contains an ambiguous dispatch")
	ErrEvidenceUnavailable = errors.New("coding relay evidence is unavailable")
	ErrNotRevoked          = errors.New("coding relay must be revoked before evidence finalization")
	ErrEvidenceBinding     = errors.New("coding relay evidence binding disagrees")
	ErrClockRollback       = errors.New("coding relay clock moved backwards")
)

// Binding is immutable ticket, harness, grant, deadline, and effective-budget
// authority for one relay. AttemptID is a certification ID or shadow-attempt
// ID chosen by the trusted gateway, never by the miner.
type Binding struct {
	AttemptID             string
	AgentArtifactSHA256   string
	HarnessInstanceID     string
	TicketID              string
	CaseID                string
	ProfileCapabilityID   string
	GrantID               string
	Generation            uint32
	InferenceGrantSHA256  string
	IssuedAt              time.Time
	Deadline              time.Time
	RequestBudget         uint32
	PromptTokenBudget     uint64
	CompletionTokenBudget uint64
}

// EvidenceBinding is the caller-supplied source identity checked at evidence
// finalization. GrantID and generation remain relay-owned live-grant state.
type EvidenceBinding struct {
	AttemptID             string
	AgentArtifactSHA256   string
	HarnessInstanceID     string
	TicketID              string
	CaseID                string
	ProfileCapabilityID   string
	InferenceGrantSHA256  string
	Deadline              time.Time
	RequestBudget         uint32
	PromptTokenBudget     uint64
	CompletionTokenBudget uint64
}

// Config constructs one relay without wiring a listener or credential.
type Config struct {
	Policy           codingcontract.InferencePolicy
	Binding          Binding
	Upstream         Upstream
	Journal          Journal
	Now              func() time.Time
	NewRequestID     func() string
	OperationTimeout time.Duration
}

// Upstream receives only a typed locked request and immutable dispatch
// authority. HTTP headers, miner bearer values, and raw routes are absent.
type Upstream interface {
	Complete(context.Context, UpstreamRequest) (UpstreamResult, error)
}

// UpstreamRequest is one durably journaled provider attempt. LockedRequest is
// deep-owned by the request and must be forwarded without adding model-visible
// fields excluded from its locked digest.
type UpstreamRequest struct {
	Sequence            uint32
	RequestSequence     uint32
	Attempt             uint32
	RequestID           string
	LockedRequestSHA256 string
	LockedRequest       codingcontract.InferenceLockedRequest
	Deadline            time.Time
}

// UpstreamResult contains a trusted Platform settlement projection. A complete
// outcome must contain normalized provider-response bytes. A failure with a
// canonical_json_v1 response digest must instead contain the exact canonical
// failure projection; no projection is forwarded to the miner.
type UpstreamResult struct {
	Settlement                codingcontract.InferenceProviderSettlement
	NormalizedResponse        []byte
	FailureResponseProjection []byte
}

// Journal durably records dispatch before provider activity, then atomically
// records the terminal settlement/receipt before any miner response. Every
// method must be idempotent for identical bytes and fail closed on conflicts.
type Journal interface {
	Load(context.Context, Binding) (JournalSnapshot, error)
	Begin(context.Context, Binding, DispatchRecord) error
	Complete(context.Context, Binding, JournalEntry) error
	Revoke(context.Context, Binding) error
}

// JournalSnapshot is replayed during relay construction. At most the final
// entry may be dispatch-only; that state is deliberately non-rerunnable.
type JournalSnapshot struct {
	Binding *Binding
	Revoked bool
	Entries []JournalEntry
}

// DispatchRecord is the durable pre-provider journal record. LockedRequest and
// MinerRequestSHA256 may commit private task/memory content and must be stored
// under the validator-local private-evidence boundary.
type DispatchRecord struct {
	Sequence            uint32
	RequestSequence     uint32
	Attempt             uint32
	RequestID           string
	MinerRequestSHA256  string
	MinerRequest        codingcontract.InferenceMinerRequest
	LockedRequestSHA256 string
	LockedRequest       codingcontract.InferenceLockedRequest
}

// JournalEntry is either an incomplete dispatch (Completed=false) or its exact
// durable completion. Response projections are private evidence;
// MinerResponse is present only for complete outcomes.
type JournalEntry struct {
	Dispatch                  DispatchRecord
	Completed                 bool
	Settlement                codingcontract.InferenceProviderSettlement
	Receipt                   codingcontract.InferenceReceipt
	NormalizedResponse        []byte
	FailureResponseProjection []byte
	MinerResponse             []byte
}

func (binding Binding) String() string { return "CodingRelayBinding{private}" }

func (binding Binding) GoString() string { return binding.String() }

func (binding Binding) LogValue() slog.Value { return slog.StringValue("coding-relay-binding") }

func (snapshot JournalSnapshot) String() string {
	return "CodingRelayJournalSnapshot{entries=" + strconv.Itoa(len(snapshot.Entries)) +
		" revoked=" + strconv.FormatBool(snapshot.Revoked) + "}"
}

func (snapshot JournalSnapshot) GoString() string { return snapshot.String() }

func (snapshot JournalSnapshot) LogValue() slog.Value {
	return slog.GroupValue(
		slog.Int("entries", len(snapshot.Entries)),
		slog.Bool("revoked", snapshot.Revoked),
	)
}

func (dispatch DispatchRecord) String() string {
	return "CodingRelayDispatch{sequence=" + strconv.FormatUint(uint64(dispatch.Sequence), 10) +
		" attempt=" + strconv.FormatUint(uint64(dispatch.Attempt), 10) + "}"
}

func (dispatch DispatchRecord) GoString() string { return dispatch.String() }

func (dispatch DispatchRecord) LogValue() slog.Value {
	return slog.GroupValue(
		slog.Uint64("sequence", uint64(dispatch.Sequence)),
		slog.Uint64("attempt", uint64(dispatch.Attempt)),
	)
}

func (entry JournalEntry) String() string {
	return "CodingRelayJournalEntry{sequence=" + strconv.FormatUint(uint64(entry.Dispatch.Sequence), 10) +
		" completed=" + strconv.FormatBool(entry.Completed) + "}"
}

func (entry JournalEntry) GoString() string { return entry.String() }

func (entry JournalEntry) LogValue() slog.Value {
	return slog.GroupValue(
		slog.Uint64("sequence", uint64(entry.Dispatch.Sequence)),
		slog.Bool("completed", entry.Completed),
	)
}

func (request UpstreamRequest) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding relay upstream requests cannot be serialized as diagnostics")
}

func (request UpstreamRequest) String() string {
	return "CodingRelayUpstreamRequest{sequence=" + strconv.FormatUint(uint64(request.Sequence), 10) +
		" attempt=" + strconv.FormatUint(uint64(request.Attempt), 10) + "}"
}

func (request UpstreamRequest) GoString() string { return request.String() }

func (request UpstreamRequest) LogValue() slog.Value {
	return slog.GroupValue(
		slog.Uint64("sequence", uint64(request.Sequence)),
		slog.Uint64("attempt", uint64(request.Attempt)),
	)
}

func (result UpstreamResult) String() string {
	return "CodingRelayUpstreamResult{outcome=" + strconv.Quote(string(result.Settlement.Outcome)) + "}"
}

func (result UpstreamResult) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding relay upstream results cannot be serialized as diagnostics")
}

func (result UpstreamResult) GoString() string { return result.String() }

func (result UpstreamResult) LogValue() slog.Value {
	return slog.StringValue(string(result.Settlement.Outcome))
}

func cloneBinding(value Binding) Binding {
	value.IssuedAt = value.IssuedAt.UTC()
	value.Deadline = value.Deadline.UTC()
	return value
}

func cloneEvidenceBinding(value EvidenceBinding) EvidenceBinding {
	value.Deadline = value.Deadline.UTC()
	return value
}

func cloneUpstreamRequest(value UpstreamRequest) UpstreamRequest {
	value.LockedRequest = cloneLockedRequest(value.LockedRequest)
	value.Deadline = value.Deadline.UTC()
	return value
}

func cloneUpstreamResult(value UpstreamResult) UpstreamResult {
	value.Settlement = value.Settlement.Clone()
	value.NormalizedResponse = append([]byte(nil), value.NormalizedResponse...)
	value.FailureResponseProjection = append([]byte(nil), value.FailureResponseProjection...)
	return value
}

func cloneDispatch(value DispatchRecord) DispatchRecord {
	value.MinerRequest = cloneMinerRequest(value.MinerRequest)
	value.LockedRequest = cloneLockedRequest(value.LockedRequest)
	return value
}

func cloneEntry(value JournalEntry) JournalEntry {
	value.Dispatch = cloneDispatch(value.Dispatch)
	value.Settlement = value.Settlement.Clone()
	value.Receipt = cloneReceipt(value.Receipt)
	value.NormalizedResponse = append([]byte(nil), value.NormalizedResponse...)
	value.FailureResponseProjection = append([]byte(nil), value.FailureResponseProjection...)
	value.MinerResponse = append([]byte(nil), value.MinerResponse...)
	return value
}

func cloneSnapshot(value JournalSnapshot) JournalSnapshot {
	if value.Binding != nil {
		binding := cloneBinding(*value.Binding)
		value.Binding = &binding
	}
	entries := value.Entries
	value.Entries = make([]JournalEntry, len(entries))
	for index, entry := range entries {
		value.Entries[index] = cloneEntry(entry)
	}
	return value
}

func cloneLockedRequest(value codingcontract.InferenceLockedRequest) codingcontract.InferenceLockedRequest {
	value.Messages = cloneRawMessages(value.Messages)
	value.Tools = cloneTools(value.Tools)
	value.Provider.Only = append([]string(nil), value.Provider.Only...)
	value.Provider.Order = append([]string(nil), value.Provider.Order...)
	return value
}

func cloneMinerRequest(value codingcontract.InferenceMinerRequest) codingcontract.InferenceMinerRequest {
	value.Messages = cloneRawMessages(value.Messages)
	value.Tools = cloneTools(value.Tools)
	return value
}

func cloneRawMessages(values []json.RawMessage) []json.RawMessage {
	if values == nil {
		return nil
	}
	result := make([]json.RawMessage, len(values))
	for index, value := range values {
		result[index] = append(json.RawMessage(nil), value...)
	}
	return result
}

func cloneTools(values []codingcontract.InferenceTool) []codingcontract.InferenceTool {
	if values == nil {
		return nil
	}
	result := make([]codingcontract.InferenceTool, len(values))
	for index, value := range values {
		result[index] = value
		result[index].Function.Parameters = append(json.RawMessage(nil), value.Function.Parameters...)
	}
	return result
}

func cloneReceipt(value codingcontract.InferenceReceipt) codingcontract.InferenceReceipt {
	value.FailureCode = cloneString(value.FailureCode)
	value.ResponseSHA256 = cloneString(value.ResponseSHA256)
	value.ProviderGenerationID = cloneString(value.ProviderGenerationID)
	value.ReceiptProvider = cloneString(value.ReceiptProvider)
	return value
}

func cloneString(value *string) *string {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}
