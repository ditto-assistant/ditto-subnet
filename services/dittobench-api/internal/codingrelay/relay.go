package codingrelay

import (
	"context"
	"encoding/json"
	"log/slog"
	"reflect"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

type flight struct {
	minerRequestSHA256 string
	done               chan struct{}
}

// Relay is one source-bound, serial coding-model capability.
type Relay struct {
	mu       sync.Mutex
	revokeMu sync.Mutex
	clockMu  sync.Mutex

	policy           codingcontract.InferencePolicy
	binding          Binding
	upstream         Upstream
	journal          Journal
	now              func() time.Time
	newRequestID     func() string
	operationTimeout time.Duration
	lastNow          time.Time
	requestSlots     chan struct{}

	entries           []JournalEntry
	revoked           bool
	revocationDurable bool
	active            *flight
	fatal             error
}

// New reconstructs one relay from its durable journal. A dispatch without an
// exact trusted settlement is non-rerunnable and fails construction.
func New(ctx context.Context, config Config) (*Relay, error) {
	if ctx == nil {
		return nil, ErrInvalidConfig
	}
	now := time.Now().UTC()
	if config.Now != nil {
		now = config.Now().UTC()
	}
	validated, err := validateConfig(config, now)
	if err != nil {
		return nil, err
	}
	relay := &Relay{
		policy: validated.Policy, binding: validated.Binding,
		upstream: validated.Upstream, journal: validated.Journal,
		now: validated.Now, newRequestID: validated.NewRequestID,
		operationTimeout: validated.OperationTimeout, lastNow: now,
		requestSlots: make(chan struct{}, 2),
	}
	loadContext, cancel, err := relay.operationContext(ctx, false)
	if err != nil {
		return nil, err
	}
	snapshot, loadErr := relay.journal.Load(loadContext, cloneBinding(relay.binding))
	callErr := loadContext.Err()
	cancel()
	if loadErr != nil || callErr != nil {
		return nil, ErrJournalUnavailable
	}
	if len(snapshot.Entries) > int(relay.policy.MaxRequests)+int(relay.policy.MaxRetries) {
		return nil, ErrEvidenceUnavailable
	}
	snapshot = cloneSnapshot(snapshot)
	if err := relay.restore(snapshot); err != nil {
		return nil, err
	}
	return relay, nil
}

// Complete parses one miner request, durably journals it, executes serial
// trusted attempts, and returns only the miner-safe response projection.
// Client cancellation is honored before admission. After durable admission the
// provider attempt is detached so response loss cannot erase its settlement.
func (relay *Relay) Complete(ctx context.Context, body []byte) ([]byte, error) {
	if relay == nil || ctx == nil {
		return nil, ErrInvalidRequest
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if !relay.acquireRequest() {
		return nil, ErrConcurrentRequest
	}
	defer relay.releaseRequest()
	if uint64(len(body)) > relay.policy.MaxRequestBytes {
		return nil, ErrInvalidRequest
	}
	return relay.completeRequest(ctx, append([]byte(nil), body...))
}

func (relay *Relay) completeRequest(ctx context.Context, body []byte) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	request, err := codingcontract.ParseInferenceMinerRequest(body, relay.policy)
	if err != nil {
		return nil, ErrInvalidRequest
	}
	minerSHA256, err := codingcontract.InferenceMinerRequestSHA256(relay.policy, request)
	if err != nil {
		return nil, ErrInvalidRequest
	}

	for {
		dispatch, replay, wait, err := relay.admit(request, minerSHA256)
		if err != nil {
			return nil, err
		}
		if replay != nil {
			return replay, nil
		}
		if wait != nil {
			select {
			case <-wait:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		return relay.execute(dispatch)
	}
}

func (relay *Relay) admit(
	request codingcontract.InferenceMinerRequest,
	minerSHA256 string,
) (DispatchRecord, []byte, <-chan struct{}, error) {
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if relay.fatal != nil {
		return DispatchRecord{}, nil, nil, relay.fatal
	}
	if relay.revoked {
		return DispatchRecord{}, nil, nil, ErrCapabilityRevoked
	}
	now, err := relay.trustedNow()
	if err != nil {
		relay.fatal = err
		return DispatchRecord{}, nil, nil, err
	}
	if !relay.binding.Deadline.After(now) {
		return DispatchRecord{}, nil, nil, ErrCapabilityExpired
	}
	if relay.active != nil {
		if relay.active.minerRequestSHA256 == minerSHA256 {
			return DispatchRecord{}, nil, relay.active.done, nil
		}
		return DispatchRecord{}, nil, nil, ErrConcurrentRequest
	}
	if len(relay.entries) > 0 {
		last := relay.entries[len(relay.entries)-1]
		if last.Receipt.Outcome == codingcontract.InferenceReceiptProviderFailed {
			return DispatchRecord{}, nil, nil, ErrProviderFailure
		}
		if last.Receipt.Outcome == codingcontract.InferenceReceiptComplete &&
			last.Dispatch.MinerRequestSHA256 == minerSHA256 && len(last.MinerResponse) != 0 {
			return DispatchRecord{}, append([]byte(nil), last.MinerResponse...), nil, nil
		}
	}
	requests, prompt, completion, cost, ok := relay.accounting()
	if !ok || requests >= uint64(relay.binding.RequestBudget) ||
		prompt >= relay.binding.PromptTokenBudget || completion >= relay.binding.CompletionTokenBudget ||
		cost >= relay.binding.CostBudgetUSDMicros {
		return DispatchRecord{}, nil, nil, ErrBudgetExhausted
	}
	remainingCompletion := relay.binding.CompletionTokenBudget - completion
	effectiveRequest := cloneMinerRequest(request)
	if effectiveRequest.MaxCompletionTokens > remainingCompletion {
		effectiveRequest.MaxCompletionTokens = remainingCompletion
	}
	locked, err := codingcontract.LockInferenceRequest(relay.policy, effectiveRequest)
	if err != nil {
		return DispatchRecord{}, nil, nil, ErrInvalidRequest
	}
	lockedSHA256, err := codingcontract.InferenceLockedRequestSHA256(relay.policy, locked)
	if err != nil {
		return DispatchRecord{}, nil, nil, ErrInvalidRequest
	}
	requestID := relay.newRequestID()
	if !canonicalUUID(requestID) {
		relay.fatal = ErrInvalidConfig
		return DispatchRecord{}, nil, nil, ErrInvalidConfig
	}
	for _, entry := range relay.entries {
		if entry.Dispatch.RequestID == requestID {
			relay.fatal = ErrInvalidConfig
			return DispatchRecord{}, nil, nil, ErrInvalidConfig
		}
	}
	dispatch := DispatchRecord{
		Sequence: uint32(len(relay.entries) + 1), RequestSequence: uint32(requests + 1), Attempt: 1,
		RequestID: requestID, MinerRequestSHA256: minerSHA256, MinerRequest: cloneMinerRequest(request),
		LockedRequestSHA256: lockedSHA256, LockedRequest: cloneLockedRequest(locked),
	}
	relay.active = &flight{minerRequestSHA256: minerSHA256, done: make(chan struct{})}
	return dispatch, nil, nil, nil
}

func (relay *Relay) execute(dispatch DispatchRecord) ([]byte, error) {
	if err := relay.begin(dispatch); err != nil {
		relay.finishFlight(err)
		return nil, err
	}
	for {
		result, err := relay.callUpstream(dispatch)
		if err != nil {
			relay.finishFlight(err)
			return nil, err
		}
		entry, response, err := relay.entryForResult(dispatch, result)
		if err != nil {
			relay.finishFlight(err)
			return nil, err
		}
		if err := relay.complete(entry); err != nil {
			relay.finishFlight(err)
			return nil, err
		}
		if entry.Receipt.Outcome == codingcontract.InferenceReceiptFreeRetry {
			now, clockErr := relay.trustedNow()
			if clockErr != nil {
				relay.finishFlight(clockErr)
				return nil, clockErr
			}
			if dispatch.Attempt >= relay.policy.MaxAttemptsPerRequest || !relay.binding.Deadline.After(now) {
				relay.finishFlight(ErrEvidenceUnavailable)
				return nil, ErrEvidenceUnavailable
			}
			dispatch.Sequence++
			dispatch.Attempt++
			if err := relay.begin(dispatch); err != nil {
				relay.finishFlight(err)
				return nil, err
			}
			continue
		}
		if err := relay.validateTerminalAccounting(); err != nil {
			relay.finishFlight(err)
			return nil, err
		}
		relay.finishFlight(nil)
		if entry.Receipt.Outcome == codingcontract.InferenceReceiptProviderFailed {
			return nil, ErrProviderFailure
		}
		return response, nil
	}
}

func (relay *Relay) begin(dispatch DispatchRecord) error {
	operationContext, cancel, err := relay.operationContext(context.Background(), true)
	if err != nil {
		return err
	}
	beginErr := relay.journal.Begin(operationContext, cloneBinding(relay.binding), cloneDispatch(dispatch))
	callErr := operationContext.Err()
	cancel()
	if beginErr != nil || callErr != nil {
		return ErrJournalUnavailable
	}
	return nil
}

func (relay *Relay) complete(entry JournalEntry) error {
	operationContext, cancel, err := relay.operationContext(context.Background(), true)
	if err != nil {
		return err
	}
	completeErr := relay.journal.Complete(operationContext, cloneBinding(relay.binding), cloneEntry(entry))
	callErr := operationContext.Err()
	cancel()
	if completeErr != nil || callErr != nil {
		return ErrJournalUnavailable
	}
	relay.mu.Lock()
	if len(relay.entries) != 0 {
		relay.entries[len(relay.entries)-1].MinerResponse = nil
	}
	relay.entries = append(relay.entries, compactEntry(entry, true))
	relay.mu.Unlock()
	return nil
}

func (relay *Relay) callUpstream(dispatch DispatchRecord) (UpstreamResult, error) {
	callContext, cancel, err := relay.providerContext()
	if err != nil {
		return UpstreamResult{}, err
	}
	request := UpstreamRequest{
		Sequence: dispatch.Sequence, RequestSequence: dispatch.RequestSequence, Attempt: dispatch.Attempt,
		RequestID: dispatch.RequestID, LockedRequestSHA256: dispatch.LockedRequestSHA256,
		LockedRequest: cloneLockedRequest(dispatch.LockedRequest), Deadline: relay.binding.Deadline,
	}
	result, upstreamErr := relay.upstream.Complete(callContext, cloneUpstreamRequest(request))
	callErr := callContext.Err()
	cancel()
	now, clockErr := relay.trustedNow()
	if upstreamErr != nil || callErr != nil || clockErr != nil || !relay.binding.Deadline.After(now) {
		return UpstreamResult{}, ErrUpstreamUnsettled
	}
	if uint64(len(result.NormalizedResponse)) > relay.policy.MaxResponseBytes ||
		uint64(len(result.FailureResponseProjection)) > relay.policy.MaxResponseBytes {
		return UpstreamResult{}, ErrUpstreamUnsettled
	}
	if err := result.Settlement.Validate(relay.policy); err != nil {
		return UpstreamResult{}, ErrUpstreamUnsettled
	}
	return cloneUpstreamResult(result), nil
}

func (relay *Relay) entryForResult(
	dispatch DispatchRecord,
	result UpstreamResult,
) (JournalEntry, []byte, error) {
	settlement := result.Settlement.Clone()
	if err := settlement.Validate(relay.policy); err != nil ||
		settlement.TicketID != relay.binding.TicketID || settlement.CaseID != relay.binding.CaseID ||
		settlement.ProfileCapabilityID != relay.binding.ProfileCapabilityID ||
		settlement.InferenceGrantSHA256 != relay.binding.InferenceGrantSHA256 ||
		settlement.GrantID != relay.binding.GrantID || settlement.Generation != relay.binding.Generation ||
		settlement.RequestID != dispatch.RequestID || settlement.RequestSequence != dispatch.RequestSequence ||
		settlement.Attempt != dispatch.Attempt || settlement.LockedRequestSHA256 != dispatch.LockedRequestSHA256 ||
		settlement.CompletionTokens > dispatch.LockedRequest.MaxCompletionTokens {
		return JournalEntry{}, nil, ErrUpstreamUnsettled
	}
	settlementSHA256, err := codingcontract.InferenceProviderSettlementSHA256(relay.policy, settlement)
	if err != nil {
		return JournalEntry{}, nil, ErrUpstreamUnsettled
	}
	var minerResponse []byte
	if settlement.Outcome == codingcontract.InferenceReceiptComplete {
		if len(result.FailureResponseProjection) != 0 {
			return JournalEntry{}, nil, ErrUpstreamUnsettled
		}
		minerResponse, err = relay.minerResponse(result.NormalizedResponse, settlement)
		if err != nil {
			return JournalEntry{}, nil, err
		}
	} else {
		if len(result.NormalizedResponse) != 0 {
			return JournalEntry{}, nil, ErrUpstreamUnsettled
		}
		if settlement.ResponseSHA256 == nil {
			if len(result.FailureResponseProjection) != 0 {
				return JournalEntry{}, nil, ErrUpstreamUnsettled
			}
		} else {
			failureSHA256, digestErr := codingcontract.InferenceCanonicalResponseProjectionSHA256(
				relay.policy, result.FailureResponseProjection,
			)
			if digestErr != nil || failureSHA256 != *settlement.ResponseSHA256 {
				return JournalEntry{}, nil, ErrUpstreamUnsettled
			}
		}
	}
	providerSelected := settlement.RouterAttempts[0].Selected
	receipt := codingcontract.InferenceReceipt{
		Schema:   codingcontract.InferenceReceiptSchema,
		Sequence: dispatch.Sequence, RequestSequence: dispatch.RequestSequence, Attempt: dispatch.Attempt,
		RequestID: dispatch.RequestID, LockedRequestSHA256: dispatch.LockedRequestSHA256,
		PromptSHA256: relay.policy.PromptSHA256, ToolSchemaSHA256: relay.policy.ToolSchemaSHA256,
		Outcome: settlement.Outcome, FailureCode: cloneString(settlement.TerminalErrorCode),
		HTTPStatus: settlement.HTTPStatus, ResponseSHA256: cloneString(settlement.ResponseSHA256),
		ResponseDigestKind:       settlement.ResponseDigestKind,
		ProviderGenerationID:     cloneString(settlement.ProviderGenerationID),
		ProviderSettlementSHA256: settlementSHA256, Model: relay.policy.Model,
		ProviderRoute: relay.policy.ProviderRoute, ProviderRouteProfile: relay.policy.ProviderRouteProfile,
		ProviderSelected: providerSelected, ReceiptProvider: cloneString(settlement.ReceiptProvider),
		FallbackUsed: settlement.FallbackUsed, PromptTokens: settlement.PromptTokens,
		CompletionTokens: settlement.CompletionTokens, TotalTokens: settlement.TotalTokens,
		CostUSDMicros: settlement.CostUSDMicros, TimedOut: settlement.TimedOut,
	}
	if err := settlement.ValidateAgainstReceipt(relay.policy, receipt); err != nil {
		return JournalEntry{}, nil, ErrUpstreamUnsettled
	}
	entry := JournalEntry{
		Dispatch: cloneDispatch(dispatch), Completed: true, Settlement: settlement,
		Receipt: receipt, NormalizedResponse: append([]byte(nil), result.NormalizedResponse...),
		FailureResponseProjection: append([]byte(nil), result.FailureResponseProjection...),
		MinerResponse:             append([]byte(nil), minerResponse...),
	}
	return entry, append([]byte(nil), minerResponse...), nil
}

func (relay *Relay) minerResponse(
	body []byte,
	settlement codingcontract.InferenceProviderSettlement,
) ([]byte, error) {
	if len(body) == 0 {
		return nil, ErrUpstreamUnsettled
	}
	normalized, err := codingcontract.ParseInferenceNormalizedResponse(body, relay.policy)
	if err != nil {
		return nil, ErrUpstreamUnsettled
	}
	responseSHA256, err := codingcontract.InferenceNormalizedResponseSHA256(relay.policy, normalized)
	if err != nil || settlement.ResponseSHA256 == nil || responseSHA256 != *settlement.ResponseSHA256 ||
		settlement.ProviderGenerationID == nil || normalized.ID != *settlement.ProviderGenerationID ||
		normalized.Usage.PromptTokens != settlement.PromptTokens ||
		normalized.Usage.CompletionTokens != settlement.CompletionTokens ||
		normalized.Usage.TotalTokens != settlement.TotalTokens ||
		normalized.Usage.CostUSDMicros != settlement.CostUSDMicros {
		return nil, ErrUpstreamUnsettled
	}
	miner := codingcontract.InferenceMinerResponse{
		ID: normalized.ID, Model: normalized.Model, Choices: normalized.Choices,
		Usage: codingcontract.InferenceMinerUsage{
			PromptTokens:     normalized.Usage.PromptTokens,
			CompletionTokens: normalized.Usage.CompletionTokens,
			TotalTokens:      normalized.Usage.TotalTokens,
		},
	}
	encoded, err := json.Marshal(miner)
	if err != nil {
		return nil, ErrUpstreamUnsettled
	}
	encoded = append(encoded, '\n')
	if _, err := codingcontract.ParseInferenceMinerResponse(encoded, relay.policy); err != nil ||
		len(encoded) > int(relay.policy.MaxResponseBytes) {
		return nil, ErrUpstreamUnsettled
	}
	return encoded, nil
}

func (relay *Relay) finishFlight(fatal error) {
	relay.mu.Lock()
	if fatal != nil && relay.fatal == nil {
		relay.fatal = fatal
	}
	if relay.active != nil {
		close(relay.active.done)
		relay.active = nil
	}
	relay.mu.Unlock()
}

func (relay *Relay) validateTerminalAccounting() error {
	relay.mu.Lock()
	entries := cloneSnapshot(JournalSnapshot{Entries: relay.entries}).Entries
	relay.mu.Unlock()
	_, err := relay.evidence(entries)
	return err
}

// Revoke closes admission immediately and waits for an already-admitted
// attempt to durably settle before persisting revocation. It is idempotent.
func (relay *Relay) Revoke(ctx context.Context) error {
	if relay == nil || ctx == nil {
		return ErrInvalidConfig
	}
	relay.revokeMu.Lock()
	defer relay.revokeMu.Unlock()
	relay.mu.Lock()
	relay.revoked = true
	if relay.revocationDurable {
		relay.mu.Unlock()
		return nil
	}
	var done <-chan struct{}
	if relay.active != nil {
		done = relay.active.done
	}
	relay.mu.Unlock()
	if done != nil {
		select {
		case <-done:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	operationContext, cancel, err := relay.operationContext(ctx, false)
	if err != nil {
		return err
	}
	revokeErr := relay.journal.Revoke(operationContext, cloneBinding(relay.binding))
	callErr := operationContext.Err()
	cancel()
	if revokeErr != nil || callErr != nil {
		return ErrJournalUnavailable
	}
	relay.mu.Lock()
	relay.revocationDurable = true
	relay.mu.Unlock()
	return nil
}

// Evidence finalizes deterministic model evidence after durable revocation.
func (relay *Relay) Evidence(
	ctx context.Context,
	binding EvidenceBinding,
) (codingcontract.ModelEvidence, error) {
	if relay == nil || ctx == nil {
		return codingcontract.ModelEvidence{}, ErrEvidenceUnavailable
	}
	if err := ctx.Err(); err != nil {
		return codingcontract.ModelEvidence{}, err
	}
	binding = cloneEvidenceBinding(binding)
	relay.mu.Lock()
	if !evidenceBindingMatches(relay.binding, binding) {
		relay.mu.Unlock()
		return codingcontract.ModelEvidence{}, ErrEvidenceBinding
	}
	if !relay.revoked || !relay.revocationDurable {
		relay.mu.Unlock()
		return codingcontract.ModelEvidence{}, ErrNotRevoked
	}
	if relay.active != nil || relay.fatal != nil {
		err := relay.fatal
		if err == nil {
			err = ErrEvidenceUnavailable
		}
		relay.mu.Unlock()
		return codingcontract.ModelEvidence{}, err
	}
	entries := cloneSnapshot(JournalSnapshot{Entries: relay.entries}).Entries
	relay.mu.Unlock()
	return relay.evidence(entries)
}

func (relay *Relay) evidence(entries []JournalEntry) (codingcontract.ModelEvidence, error) {
	binding := relay.receiptBinding()
	if len(entries) == 0 {
		return codingcontract.NotInvokedInferenceModelEvidence(relay.policy, binding)
	}
	if !relay.withinBudgets(entries) {
		return codingcontract.ModelEvidence{}, ErrBudgetExhausted
	}
	set := codingcontract.InferenceReceiptSet{
		Schema: codingcontract.InferenceReceiptSetSchema, CodingContractVersion: codingcontract.ContractVersion,
		TicketID: relay.binding.TicketID, CaseID: relay.binding.CaseID,
		ProfileCapabilityID: relay.binding.ProfileCapabilityID,
		GrantID:             relay.binding.GrantID, Generation: relay.binding.Generation,
		InferenceGrantSHA256: relay.binding.InferenceGrantSHA256,
		RequestBudget:        relay.binding.RequestBudget, PromptTokenBudget: relay.binding.PromptTokenBudget,
		CompletionTokenBudget: relay.binding.CompletionTokenBudget,
		Receipts:              make([]codingcontract.InferenceReceipt, len(entries)),
	}
	settlements := make([]codingcontract.InferenceProviderSettlement, len(entries))
	for index, entry := range entries {
		if !entry.Completed {
			return codingcontract.ModelEvidence{}, ErrAmbiguousDispatch
		}
		set.Receipts[index] = cloneReceipt(entry.Receipt)
		settlements[index] = entry.Settlement.Clone()
	}
	evidence, err := codingcontract.DeriveInferenceModelEvidence(relay.policy, binding, set, settlements)
	if err != nil {
		return codingcontract.ModelEvidence{}, ErrEvidenceUnavailable
	}
	return evidence, nil
}

func (relay *Relay) receiptBinding() codingcontract.InferenceReceiptBinding {
	return codingcontract.InferenceReceiptBinding{
		TicketID: relay.binding.TicketID, CaseID: relay.binding.CaseID,
		ProfileCapabilityID: relay.binding.ProfileCapabilityID,
		GrantID:             relay.binding.GrantID, Generation: relay.binding.Generation,
		InferenceGrantSHA256: relay.binding.InferenceGrantSHA256,
		RequestBudget:        relay.binding.RequestBudget, PromptTokenBudget: relay.binding.PromptTokenBudget,
		CompletionTokenBudget: relay.binding.CompletionTokenBudget,
	}
}

func (relay *Relay) withinBudgets(entries []JournalEntry) bool {
	requests, prompt, completion, cost, ok := accountingFor(entries)
	return ok && requests <= uint64(relay.binding.RequestBudget) &&
		prompt <= relay.binding.PromptTokenBudget && completion <= relay.binding.CompletionTokenBudget &&
		cost <= relay.binding.CostBudgetUSDMicros
}

func (relay *Relay) accounting() (uint64, uint64, uint64, uint64, bool) {
	return accountingFor(relay.entries)
}

func accountingFor(entries []JournalEntry) (uint64, uint64, uint64, uint64, bool) {
	var requests, prompt, completion, cost uint64
	var previousRequest uint32
	for _, entry := range entries {
		if entry.Dispatch.RequestSequence != previousRequest {
			requests++
			previousRequest = entry.Dispatch.RequestSequence
		}
		if prompt > ^uint64(0)-entry.Receipt.PromptTokens ||
			completion > ^uint64(0)-entry.Receipt.CompletionTokens ||
			cost > ^uint64(0)-entry.Receipt.CostUSDMicros {
			return 0, 0, 0, 0, false
		}
		prompt += entry.Receipt.PromptTokens
		completion += entry.Receipt.CompletionTokens
		cost += entry.Receipt.CostUSDMicros
	}
	return requests, prompt, completion, cost, true
}

func (relay *Relay) restore(snapshot JournalSnapshot) error {
	if snapshot.Binding == nil {
		if snapshot.Revoked || len(snapshot.Entries) != 0 {
			return ErrEvidenceBinding
		}
	} else if !bindingMatches(relay.binding, *snapshot.Binding) {
		return ErrEvidenceBinding
	}
	var priorCompletion uint64
	for index, entry := range snapshot.Entries {
		if !entry.Completed {
			return ErrAmbiguousDispatch
		}
		if entry.Dispatch.Sequence != uint32(index+1) ||
			entry.Receipt.Sequence != entry.Dispatch.Sequence ||
			entry.Receipt.RequestSequence != entry.Dispatch.RequestSequence ||
			entry.Receipt.Attempt != entry.Dispatch.Attempt || entry.Receipt.RequestID != entry.Dispatch.RequestID ||
			entry.Receipt.LockedRequestSHA256 != entry.Dispatch.LockedRequestSHA256 ||
			!canonicalUUID(entry.Dispatch.RequestID) {
			return ErrEvidenceUnavailable
		}
		minerSHA256, err := codingcontract.InferenceMinerRequestSHA256(relay.policy, entry.Dispatch.MinerRequest)
		if err != nil || minerSHA256 != entry.Dispatch.MinerRequestSHA256 ||
			priorCompletion >= relay.binding.CompletionTokenBudget {
			return ErrEvidenceUnavailable
		}
		effectiveMiner := cloneMinerRequest(entry.Dispatch.MinerRequest)
		if remaining := relay.binding.CompletionTokenBudget - priorCompletion; effectiveMiner.MaxCompletionTokens > remaining {
			effectiveMiner.MaxCompletionTokens = remaining
		}
		expectedLocked, err := codingcontract.LockInferenceRequest(relay.policy, effectiveMiner)
		if err != nil || !reflect.DeepEqual(expectedLocked, entry.Dispatch.LockedRequest) {
			return ErrEvidenceUnavailable
		}
		lockedSHA256, err := codingcontract.InferenceLockedRequestSHA256(relay.policy, entry.Dispatch.LockedRequest)
		if err != nil || lockedSHA256 != entry.Dispatch.LockedRequestSHA256 {
			return ErrEvidenceUnavailable
		}
		expected, _, err := relay.entryForResult(entry.Dispatch, UpstreamResult{
			Settlement: entry.Settlement, NormalizedResponse: entry.NormalizedResponse,
			FailureResponseProjection: entry.FailureResponseProjection,
		})
		if err != nil || !reflect.DeepEqual(expected, entry) {
			return ErrEvidenceUnavailable
		}
		if priorCompletion > ^uint64(0)-entry.Receipt.CompletionTokens {
			return ErrEvidenceUnavailable
		}
		priorCompletion += entry.Receipt.CompletionTokens
	}
	if len(snapshot.Entries) > 0 {
		last := snapshot.Entries[len(snapshot.Entries)-1]
		if last.Receipt.Outcome == codingcontract.InferenceReceiptFreeRetry {
			return ErrAmbiguousDispatch
		}
		if _, err := relay.evidence(snapshot.Entries); err != nil {
			return err
		}
	}
	relay.entries = make([]JournalEntry, len(snapshot.Entries))
	for index, entry := range snapshot.Entries {
		relay.entries[index] = compactEntry(entry, index == len(snapshot.Entries)-1)
	}
	relay.revoked = snapshot.Revoked
	relay.revocationDurable = snapshot.Revoked
	return nil
}

func (relay *Relay) providerContext() (context.Context, context.CancelFunc, error) {
	now, err := relay.trustedNow()
	if err != nil {
		return nil, nil, err
	}
	duration := time.Duration(relay.policy.RequestTimeoutMilliseconds) * time.Millisecond
	if remaining := relay.binding.Deadline.Sub(now); remaining < duration {
		duration = remaining
	}
	if duration <= 0 {
		return nil, nil, ErrCapabilityExpired
	}
	ctx, cancel := context.WithTimeout(context.Background(), duration)
	return ctx, cancel, nil
}

func compactEntry(entry JournalEntry, keepMinerResponse bool) JournalEntry {
	entry = cloneEntry(entry)
	entry.Dispatch.MinerRequest = codingcontract.InferenceMinerRequest{}
	entry.Dispatch.LockedRequest = codingcontract.InferenceLockedRequest{}
	entry.NormalizedResponse = nil
	entry.FailureResponseProjection = nil
	if !keepMinerResponse {
		entry.MinerResponse = nil
	}
	return entry
}

func (relay *Relay) acquireRequest() bool {
	select {
	case relay.requestSlots <- struct{}{}:
		return true
	default:
		return false
	}
}

func (relay *Relay) releaseRequest() { <-relay.requestSlots }

func (relay *Relay) trustedNow() (time.Time, error) {
	relay.clockMu.Lock()
	defer relay.clockMu.Unlock()
	now := relay.now().UTC()
	if now.IsZero() || now.Before(relay.lastNow) {
		return time.Time{}, ErrClockRollback
	}
	relay.lastNow = now
	return now, nil
}

func (relay *Relay) operationContext(
	parent context.Context,
	capAtBindingDeadline bool,
) (context.Context, context.CancelFunc, error) {
	if parent == nil {
		return nil, nil, ErrInvalidConfig
	}
	if err := parent.Err(); err != nil {
		return nil, nil, err
	}
	now := relay.now().UTC()
	duration := relay.operationTimeout
	if parentDeadline, ok := parent.Deadline(); ok {
		if remaining := time.Until(parentDeadline); remaining < duration {
			duration = remaining
		}
	}
	if capAtBindingDeadline {
		if remaining := relay.binding.Deadline.Sub(now); remaining < duration {
			duration = remaining
		}
	}
	if duration <= 0 {
		return nil, nil, context.DeadlineExceeded
	}
	ctx, cancel := context.WithTimeout(parent, duration)
	return ctx, cancel, nil
}

func (relay *Relay) String() string { return "CodingRelay{private}" }

func (relay *Relay) GoString() string { return relay.String() }

func (relay *Relay) LogValue() slog.Value { return slog.StringValue("coding-relay") }
