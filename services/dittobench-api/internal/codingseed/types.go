package codingseed

import (
	"context"
	"errors"
	"log/slog"
	"strconv"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

const maximumBundleBytes int64 = codingcontract.MaxCanonicalJSONBytes

type Config struct {
	MaxBundleBytes int64
	SeedTimeout    time.Duration
	Now            func() time.Time
}

type Binding struct {
	TicketID            string
	CaseID              string
	ProfileCapabilityID string
	MemoryBundleSHA256  string
	Deadline            time.Time
}

type Projector struct {
	maximum int64
	timeout time.Duration
	now     func() time.Time
}

type Projection struct {
	request  codingcontract.SeedRequest
	deadline time.Time
}

type Delivery struct {
	AlreadySeeded      bool
	MemoryBundleSHA256 string
	MemoryCount        int
}

type SeedClient interface {
	Seed(context.Context, codingcontract.SeedRequest) (codingcertifier.SeedResponse, error)
}

var _ SeedClient = (*codingcertifier.HTTPHarnessClient)(nil)

func (Projection) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding seed projections cannot be serialized as diagnostics")
}

func (projection Projection) String() string {
	return "CodingSeedProjection{case=" + strconv.Quote(projection.request.CaseID) +
		" memories=" + strconv.Itoa(len(projection.request.Memories)) + "}"
}

func (projection Projection) GoString() string { return projection.String() }

func (projection Projection) LogValue() slog.Value {
	return slog.GroupValue(
		slog.String("case", projection.request.CaseID),
		slog.Int("memories", len(projection.request.Memories)),
	)
}

func (projection Projection) Request() codingcontract.SeedRequest {
	return cloneSeedRequest(projection.request)
}

func (projection Projection) ValidateBinding(binding Binding) error {
	binding.Deadline = binding.Deadline.UTC()
	if !projection.deadline.Equal(binding.Deadline) ||
		projection.request.TicketID != binding.TicketID || projection.request.CaseID != binding.CaseID ||
		projection.request.ProfileCapabilityID != binding.ProfileCapabilityID ||
		projection.request.MemoryBundleSHA256 != binding.MemoryBundleSHA256 {
		return errors.New("coding seed projection binding disagrees")
	}
	return projection.request.Validate()
}
