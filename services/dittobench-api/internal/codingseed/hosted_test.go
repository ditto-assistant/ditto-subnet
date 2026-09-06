package codingseed

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

type hostedSeedFunc func(context.Context, codingcontract.HostedSeedRequest) (codingcertifier.SeedResponse, error)

func (f hostedSeedFunc) Seed(ctx context.Context, request codingcontract.HostedSeedRequest) (codingcertifier.SeedResponse, error) {
	return f(ctx, request)
}

func hostedBinding(now time.Time, body []byte) Binding {
	binding := fixtureBinding(now, body)
	binding.TicketID = "10000000-0000-4000-8000-000000000001"
	binding.CaseID = "20000000-0000-4000-8000-000000000002"
	binding.Deadline = now.Add(time.Minute)
	return binding
}

func TestHostedProjectionPreservesRawMemoryAndDeliversNativeRequest(t *testing.T) {
	projector, now := fixtureProjector(t)
	body, memories := fixtureArtifact(t)
	projection, err := projector.ProjectHosted(bytes.NewReader(body), hostedBinding(now, body))
	if err != nil {
		t.Fatal(err)
	}
	request := projection.Request()
	if request.CodingContractVersion != 2 || request.Validate() != nil || codingcontract.SeedRequest(request).Validate() == nil {
		t.Fatal("wrong projection version")
	}
	request.Memories[0].Content = "PRIVATE_MARKER"
	if projection.Request().Memories[0].Content != memories[0].Content {
		t.Fatal("projection is mutable")
	}
	if _, err := json.Marshal(projection); err == nil {
		t.Fatal("private projection serialized")
	}
	if strings.Contains(fmt.Sprintf("%+v %#v", projection, projection), memories[0].Content) {
		t.Fatal("memory diagnostics leaked")
	}
	called := 0
	client := hostedSeedFunc(func(_ context.Context, r codingcontract.HostedSeedRequest) (codingcertifier.SeedResponse, error) {
		called++
		if r.Validate() != nil || r.CodingContractVersion != 2 {
			t.Error("invalid native seed")
		}
		return codingcertifier.SeedResponse{CaseID: r.CaseID, ProfileCapabilityID: r.ProfileCapabilityID, MemoryBundleSHA256: r.MemoryBundleSHA256, MemoryCount: len(r.Memories)}, nil
	})
	if delivery, err := projector.DeliverHosted(t.Context(), client, projection); err != nil || delivery.MemoryCount != len(memories) || called != 1 {
		t.Fatal("native delivery failed")
	}
}

type unreadHostedMemory struct{ read bool }

func (r *unreadHostedMemory) Read([]byte) (int, error) { r.read = true; return 0, io.EOF }

func TestHostedSeedRefusesInvalidBindingBeforeReadingAndLateAcknowledgement(t *testing.T) {
	projector, now := fixtureProjector(t)
	body, _ := fixtureArtifact(t)
	binding := hostedBinding(now, body)
	bad := binding
	bad.CaseID = "private-task-label"
	reader := &unreadHostedMemory{}
	if _, err := projector.ProjectHosted(reader, bad); err == nil || reader.read {
		t.Fatal("invalid identity consumed memory")
	}
	projection, err := projector.ProjectHosted(bytes.NewReader(body), binding)
	if err != nil {
		t.Fatal(err)
	}
	projection.deadline = time.Now().Add(10 * time.Millisecond)
	client := hostedSeedFunc(func(ctx context.Context, r codingcontract.HostedSeedRequest) (codingcertifier.SeedResponse, error) {
		<-ctx.Done()
		return codingcertifier.SeedResponse{CaseID: r.CaseID, ProfileCapabilityID: r.ProfileCapabilityID, MemoryBundleSHA256: r.MemoryBundleSHA256, MemoryCount: len(r.Memories)}, nil
	})
	if _, err := projector.DeliverHosted(t.Context(), client, projection); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("late acknowledgement accepted")
	}
}
