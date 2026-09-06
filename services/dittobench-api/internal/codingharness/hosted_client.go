package codingharness

import (
	"context"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

func (handle *HostedHandle) call(ctx context.Context) (*codingcertifier.HostedHTTPHarnessClient, context.Context, context.CancelFunc, error) {
	if handle == nil || ctx == nil || ctx.Err() != nil {
		return nil, nil, nil, ErrInactive
	}
	handle.mu.Lock()
	defer handle.mu.Unlock()
	if !handle.active || handle.closed || handle.client == nil || handle.life.Err() != nil || !handle.binding.Deadline.After(handle.factory.base.now().UTC()) {
		return nil, nil, nil, ErrInactive
	}
	call, cancel := context.WithTimeout(ctx, handle.binding.Deadline.Sub(handle.factory.base.now().UTC()))
	stop := context.AfterFunc(handle.life, cancel)
	return handle.client, call, func() { stop(); cancel() }, nil
}

func (handle *HostedHandle) Health(ctx context.Context) (codingcertifier.HealthResponse, error) {
	client, call, cancel, err := handle.call(ctx)
	if err != nil {
		return codingcertifier.HealthResponse{}, err
	}
	defer cancel()
	response, err := client.Health(call)
	if call.Err() != nil || handle.life.Err() != nil {
		return codingcertifier.HealthResponse{}, ErrClosed
	}
	return response, err
}

func (handle *HostedHandle) matches(evaluation, attempt, profile string) bool {
	return handle != nil && handle.binding.EvaluationID == evaluation && handle.binding.AttemptID == attempt && handle.binding.ProfileCapabilityID == profile
}

func (handle *HostedHandle) Seed(ctx context.Context, request codingcontract.HostedSeedRequest) (codingcertifier.SeedResponse, error) {
	if !handle.matches(request.TicketID, request.CaseID, request.ProfileCapabilityID) {
		return codingcertifier.SeedResponse{}, ErrInvalid
	}
	client, call, cancel, err := handle.call(ctx)
	if err != nil {
		return codingcertifier.SeedResponse{}, err
	}
	defer cancel()
	response, err := client.Seed(call, request)
	if call.Err() != nil || handle.life.Err() != nil {
		return codingcertifier.SeedResponse{}, ErrClosed
	}
	return response, err
}

func (handle *HostedHandle) Run(ctx context.Context, request codingcontract.HostedRunRequest) (codingcertifier.RunResponse, error) {
	if !handle.matches(request.TicketID, request.CaseID, request.ProfileCapabilityID) || request.Validate() != nil {
		return codingcertifier.RunResponse{}, ErrInvalid
	}
	client, call, cancel, err := handle.call(ctx)
	if err != nil {
		return codingcertifier.RunResponse{}, err
	}
	defer cancel()
	handle.mu.Lock()
	if handle.runAttempted || handle.closed || handle.life.Err() != nil {
		handle.mu.Unlock()
		return codingcertifier.RunResponse{}, ErrClosed
	}
	// An ambiguous response never earns a second candidate execution.
	handle.runAttempted = true
	handle.mu.Unlock()
	response, err := client.Run(call, request)
	if call.Err() != nil || handle.life.Err() != nil {
		return codingcertifier.RunResponse{}, ErrClosed
	}
	return response, err
}
