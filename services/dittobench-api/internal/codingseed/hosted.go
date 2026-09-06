package codingseed

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

type HostedSeedClient interface {
	Seed(context.Context, codingcontract.HostedSeedRequest) (codingcertifier.SeedResponse, error)
}

type HostedProjection struct {
	request  codingcontract.HostedSeedRequest
	deadline time.Time
}

func (HostedProjection) MarshalJSON() ([]byte, error) {
	return nil, errors.New("hosted memory projections are private")
}
func (HostedProjection) String() string             { return "HostedSeedProjection{private=true}" }
func (value HostedProjection) GoString() string     { return value.String() }
func (value HostedProjection) LogValue() slog.Value { return slog.StringValue(value.String()) }

func (projection HostedProjection) Request() codingcontract.HostedSeedRequest {
	return codingcontract.HostedSeedRequest(cloneSeedRequest(codingcontract.SeedRequest(projection.request)))
}

// ProjectHosted reuses the version-neutral memory artifact validation, then
// constructs the native v2 request before any harness call. It does not execute
// a v1 attempt or authorize access to the source bundle.
func (projector *Projector) ProjectHosted(reader io.Reader, binding Binding) (HostedProjection, error) {
	if projector == nil || codingcontract.ValidateHostedHarnessIdentity(binding.TicketID, binding.CaseID) != nil || binding.Deadline.After(projector.now().Add(time.Hour)) {
		return HostedProjection{}, errors.New("hosted seed binding is invalid")
	}
	parsed, err := projector.Project(reader, binding)
	if err != nil {
		return HostedProjection{}, err
	}
	request := codingcontract.HostedSeedRequest(parsed.Request())
	request.CodingContractVersion = codingcontract.HostedHarnessVersion
	if err := request.Validate(); err != nil {
		return HostedProjection{}, err
	}
	return HostedProjection{request: request, deadline: binding.Deadline.UTC()}, nil
}

func (projector *Projector) DeliverHosted(ctx context.Context, client HostedSeedClient, projection HostedProjection) (Delivery, error) {
	if projector == nil || ctx == nil || nilLike(client) || projection.deadline.IsZero() {
		return Delivery{}, errors.New("hosted seed delivery is unavailable")
	}
	request := projection.Request()
	if request.Validate() != nil || !projection.deadline.After(projector.now()) || projection.deadline.After(projector.now().Add(time.Hour)) {
		return Delivery{}, errors.New("hosted seed delivery is invalid")
	}
	call, cancel, err := projector.callContext(ctx, projection.deadline)
	if err != nil {
		return Delivery{}, err
	}
	defer cancel()
	response, err := client.Seed(call, request)
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return Delivery{}, context.Canceled
		}
		if errors.Is(err, context.DeadlineExceeded) {
			return Delivery{}, context.DeadlineExceeded
		}
		return Delivery{}, errors.New("hosted seed delivery failed")
	}
	if err := call.Err(); err != nil {
		return Delivery{}, err
	}
	if err := ctx.Err(); err != nil {
		return Delivery{}, err
	}
	if !projection.deadline.After(projector.now()) {
		return Delivery{}, context.DeadlineExceeded
	}
	if response.ValidateIdentity(codingcontract.SeedRequest(request)) != nil {
		return Delivery{}, errors.New("hosted seed acknowledgement is invalid")
	}
	return Delivery{AlreadySeeded: response.IdempotentReplay, MemoryBundleSHA256: request.MemoryBundleSHA256, MemoryCount: len(request.Memories)}, nil
}
