package codingseed

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

type seedClientFunc func(context.Context, codingcontract.SeedRequest) (codingcertifier.SeedResponse, error)

func (function seedClientFunc) Seed(
	ctx context.Context,
	request codingcontract.SeedRequest,
) (codingcertifier.SeedResponse, error) {
	return function(ctx, request)
}

func fixtureProjector(t *testing.T) (*Projector, time.Time) {
	t.Helper()
	now := time.Now().UTC().Truncate(time.Second)
	projector, err := New(Config{
		MaxBundleBytes: codingcontract.MaxCanonicalJSONBytes,
		SeedTimeout:    time.Second, Now: func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	return projector, now
}

func fixtureArtifact(t *testing.T) ([]byte, []codingcontract.VisibleMemory) {
	t.Helper()
	repository, fact, from := "repository-001", "fact-001", "repository-epoch-2"
	memories := []codingcontract.VisibleMemory{{
		MemoryID: "memory-001", RepositoryCapabilityID: &repository, FactGroupID: &fact,
		Scope: "module", Type: "previous_bug_fix",
		Content:        "Preserve incomplete input between streaming parser calls.",
		ValidFromEpoch: &from, ValidUntilEpoch: nil, Supersedes: []string{}, ConfidenceMicros: 960_000,
	}}
	request := codingcontract.SeedRequest{
		CodingContractVersion: codingcontract.ContractVersion,
		TicketID:              "33333333-3333-4333-8333-333333333333", CaseID: "case-001",
		ProfileCapabilityID: "profile-001", Memories: memories,
	}
	projection := struct {
		Memories []codingcontract.VisibleMemory `json:"memories"`
	}{Memories: memories}
	body := canonicalFixture(t, projection)
	request.MemoryBundleSHA256 = digest(body)
	if err := request.Validate(); err != nil {
		t.Fatal(err)
	}
	return body, memories
}

func fixtureBinding(now time.Time, body []byte) Binding {
	return Binding{
		TicketID: "33333333-3333-4333-8333-333333333333", CaseID: "case-001",
		ProfileCapabilityID: "profile-001", MemoryBundleSHA256: digest(body), Deadline: now.Add(time.Hour),
	}
}

func TestProjectAndDeliverScopedMemoryWithoutLeakingMutableState(t *testing.T) {
	projector, now := fixtureProjector(t)
	body, memories := fixtureArtifact(t)
	projection, err := projector.Project(bytes.NewReader(body), fixtureBinding(now, body))
	if err != nil {
		t.Fatal(err)
	}
	request := projection.Request()
	if len(request.Memories) != 1 || request.Memories[0].MemoryID != memories[0].MemoryID {
		t.Fatalf("request=%#v", request)
	}
	request.Memories[0].Content = "mutated"
	request.Memories[0].Supersedes = append(request.Memories[0].Supersedes, "different")
	if projection.Request().Memories[0].Content == "mutated" || len(projection.Request().Memories[0].Supersedes) != 0 {
		t.Fatal("projection request was not deeply owned")
	}
	if _, err := json.Marshal(projection); err == nil {
		t.Fatal("projection serialized into diagnostics")
	}
	if rendered := fmt.Sprintf("%#v", projection); strings.Contains(rendered, memories[0].Content) {
		t.Fatalf("projection diagnostics leaked memory: %s", rendered)
	}
	calls := 0
	client := seedClientFunc(func(
		_ context.Context,
		got codingcontract.SeedRequest,
	) (codingcertifier.SeedResponse, error) {
		calls++
		if got.Memories[0].Content != memories[0].Content {
			t.Fatalf("delivered request=%#v", got)
		}
		return codingcertifier.SeedResponse{
			CaseID: got.CaseID, ProfileCapabilityID: got.ProfileCapabilityID,
			MemoryBundleSHA256: got.MemoryBundleSHA256, MemoryCount: len(got.Memories), IdempotentReplay: calls > 1,
		}, nil
	})
	delivery, err := projector.Deliver(t.Context(), client, projection)
	if err != nil || calls != 1 || delivery.AlreadySeeded || delivery.MemoryCount != 1 {
		t.Fatalf("delivery=%#v calls=%d err=%v", delivery, calls, err)
	}
	delivery, err = projector.Deliver(t.Context(), client, projection)
	if err != nil || calls != 2 || !delivery.AlreadySeeded {
		t.Fatalf("replayed delivery=%#v calls=%d err=%v", delivery, calls, err)
	}
}

func TestProjectAcceptsEmptyV0AndRejectsMalformedArtifacts(t *testing.T) {
	projector, now := fixtureProjector(t)
	empty := canonicalFixture(t, struct {
		Memories []codingcontract.VisibleMemory `json:"memories"`
	}{Memories: []codingcontract.VisibleMemory{}})
	projection, err := projector.Project(bytes.NewReader(empty), fixtureBinding(now, empty))
	if err != nil || projection.Request().Memories == nil || len(projection.Request().Memories) != 0 {
		t.Fatalf("empty projection=%#v err=%v", projection, err)
	}
	valid, _ := fixtureArtifact(t)
	cases := map[string][]byte{
		"null memories":    []byte(`{"memories":null}`),
		"missing memories": []byte(`{}`),
		"duplicate root":   bytes.Replace(valid, []byte(`{"memories":`), []byte(`{"memories":[],"memories":`), 1),
		"duplicate nested": bytes.Replace(valid, []byte(`"memory_id":`), []byte(`"memory_id":"other","memory_id":`), 1),
		"missing nullable": bytes.Replace(valid, []byte(`"fact_group_id":"fact-001",`), nil, 1),
		"unknown identity": bytes.Replace(valid, []byte(`{"memories":`), []byte(`{"ticket_id":"attacker","memories":`), 1),
		"trailing":         append(append([]byte(nil), valid...), []byte(` {}`)...),
		"invalid utf8":     append([]byte(`{"memories":[]}`), 0xff),
	}
	for name, value := range cases {
		t.Run(name, func(t *testing.T) {
			binding := fixtureBinding(now, value)
			if _, err := projector.Project(bytes.NewReader(value), binding); err == nil {
				t.Fatal("malformed memory artifact accepted")
			}
		})
	}
	binding := fixtureBinding(now, valid)
	binding.MemoryBundleSHA256 = strings.Repeat("f", 64)
	if _, err := projector.Project(bytes.NewReader(valid), binding); err == nil {
		t.Fatal("digest drift accepted")
	}
	oversized := bytes.Repeat([]byte(" "), codingcontract.MaxCanonicalJSONBytes+1)
	if _, err := projector.Project(bytes.NewReader(oversized), fixtureBinding(now, oversized)); err == nil {
		t.Fatal("oversized memory artifact accepted")
	}
}

func TestProjectRejectsNoncanonicalAndInvalidMemorySemantics(t *testing.T) {
	projector, now := fixtureProjector(t)
	body, memories := fixtureArtifact(t)
	var pretty bytes.Buffer
	if err := json.Indent(&pretty, bytes.TrimSpace(body), "", "  "); err != nil {
		t.Fatal(err)
	}
	pretty.WriteByte('\n')
	if _, err := projector.Project(bytes.NewReader(pretty.Bytes()), fixtureBinding(now, pretty.Bytes())); err == nil {
		t.Fatal("noncanonical artifact accepted")
	}
	duplicate := append(cloneMemories(memories), memories[0])
	invalid := canonicalFixture(t, struct {
		Memories []codingcontract.VisibleMemory `json:"memories"`
	}{Memories: duplicate})
	if _, err := projector.Project(bytes.NewReader(invalid), fixtureBinding(now, invalid)); err == nil {
		t.Fatal("duplicate memory IDs accepted")
	}
	self := cloneMemories(memories)
	self[0].Supersedes = []string{self[0].MemoryID}
	invalid = canonicalFixture(t, struct {
		Memories []codingcontract.VisibleMemory `json:"memories"`
	}{Memories: self})
	if _, err := projector.Project(bytes.NewReader(invalid), fixtureBinding(now, invalid)); err == nil {
		t.Fatal("self-superseding memory accepted")
	}
	many := make([]codingcontract.VisibleMemory, 129)
	for index := range many {
		many[index] = memories[0]
		many[index].MemoryID = fmt.Sprintf("memory-%03d", index)
	}
	invalid = canonicalFixture(t, struct {
		Memories []codingcontract.VisibleMemory `json:"memories"`
	}{Memories: many})
	if _, err := projector.Project(bytes.NewReader(invalid), fixtureBinding(now, invalid)); err == nil {
		t.Fatal("oversized memory count accepted")
	}
}

func TestProjectionValidateBindingRejectsEveryAuthorityDrift(t *testing.T) {
	projector, now := fixtureProjector(t)
	body, _ := fixtureArtifact(t)
	binding := fixtureBinding(now, body)
	projection, err := projector.Project(bytes.NewReader(body), binding)
	if err != nil {
		t.Fatal(err)
	}
	if err := projection.ValidateBinding(binding); err != nil {
		t.Fatalf("matching binding failed: %v", err)
	}
	for name, mutate := range map[string]func(*Binding){
		"ticket":   func(value *Binding) { value.TicketID = "44444444-4444-4444-8444-444444444444" },
		"case":     func(value *Binding) { value.CaseID = "case-other" },
		"profile":  func(value *Binding) { value.ProfileCapabilityID = "profile-other" },
		"digest":   func(value *Binding) { value.MemoryBundleSHA256 = strings.Repeat("f", 64) },
		"deadline": func(value *Binding) { value.Deadline = value.Deadline.Add(time.Second) },
	} {
		t.Run(name, func(t *testing.T) {
			drifted := binding
			mutate(&drifted)
			if err := projection.ValidateBinding(drifted); err == nil {
				t.Fatal("drifted projection binding accepted")
			}
		})
	}
}

func TestDeliverRejectsAckDriftAndHonorsDeadline(t *testing.T) {
	projector, now := fixtureProjector(t)
	body, _ := fixtureArtifact(t)
	projection, err := projector.Project(bytes.NewReader(body), fixtureBinding(now, body))
	if err != nil {
		t.Fatal(err)
	}
	_, err = projector.Deliver(t.Context(), seedClientFunc(func(
		_ context.Context,
		request codingcontract.SeedRequest,
	) (codingcertifier.SeedResponse, error) {
		return codingcertifier.SeedResponse{
			CaseID: "different", ProfileCapabilityID: request.ProfileCapabilityID,
			MemoryBundleSHA256: request.MemoryBundleSHA256, MemoryCount: len(request.Memories),
		}, nil
	}), projection)
	if err == nil {
		t.Fatal("drifted seed acknowledgement accepted")
	}
	projector.now = func() time.Time { return projection.deadline }
	called := false
	_, err = projector.Deliver(t.Context(), seedClientFunc(func(
		context.Context,
		codingcontract.SeedRequest,
	) (codingcertifier.SeedResponse, error) {
		called = true
		return codingcertifier.SeedResponse{}, nil
	}), projection)
	if err == nil || called {
		t.Fatalf("expired delivery called client=%v err=%v", called, err)
	}
}

func TestDeliverRejectsClientSuccessAfterLeaseExpiry(t *testing.T) {
	projector, now := fixtureProjector(t)
	body, _ := fixtureArtifact(t)
	projection, err := projector.Project(bytes.NewReader(body), fixtureBinding(now, body))
	if err != nil {
		t.Fatal(err)
	}
	current := now
	projector.now = func() time.Time { return current }
	_, err = projector.Deliver(t.Context(), seedClientFunc(func(
		_ context.Context,
		request codingcontract.SeedRequest,
	) (codingcertifier.SeedResponse, error) {
		current = projection.deadline
		return codingcertifier.SeedResponse{
			CaseID: request.CaseID, ProfileCapabilityID: request.ProfileCapabilityID,
			MemoryBundleSHA256: request.MemoryBundleSHA256, MemoryCount: len(request.Memories),
		}, nil
	}), projection)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("post-deadline client success error=%v", err)
	}
}

func TestDeliverRejectsClientSuccessAfterCallTimeout(t *testing.T) {
	projector, now := fixtureProjector(t)
	projector.timeout = time.Millisecond
	body, _ := fixtureArtifact(t)
	projection, err := projector.Project(bytes.NewReader(body), fixtureBinding(now, body))
	if err != nil {
		t.Fatal(err)
	}
	_, err = projector.Deliver(t.Context(), seedClientFunc(func(
		ctx context.Context,
		request codingcontract.SeedRequest,
	) (codingcertifier.SeedResponse, error) {
		<-ctx.Done()
		return codingcertifier.SeedResponse{
			CaseID: request.CaseID, ProfileCapabilityID: request.ProfileCapabilityID,
			MemoryBundleSHA256: request.MemoryBundleSHA256, MemoryCount: len(request.Memories),
		}, nil
	}), projection)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("post-timeout client success error=%v", err)
	}
}

func TestProjectRejectsBindingAliasesAndTypedNilClient(t *testing.T) {
	projector, now := fixtureProjector(t)
	body, _ := fixtureArtifact(t)
	binding := fixtureBinding(now, body)
	binding.TicketID = "{" + binding.TicketID + "}"
	if _, err := projector.Project(bytes.NewReader(body), binding); err == nil {
		t.Fatal("UUID alias accepted")
	}
	projection, err := projector.Project(bytes.NewReader(body), fixtureBinding(now, body))
	if err != nil {
		t.Fatal(err)
	}
	var client *nilSeedClient
	if _, err := projector.Deliver(t.Context(), client, projection); err == nil {
		t.Fatal("typed-nil client accepted")
	}
}

type nilSeedClient struct{}

func (*nilSeedClient) Seed(context.Context, codingcontract.SeedRequest) (codingcertifier.SeedResponse, error) {
	return codingcertifier.SeedResponse{}, errors.New("unreachable")
}

func canonicalFixture(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projection any
	if err := decoder.Decode(&projection); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projection); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func digest(body []byte) string {
	value := sha256.Sum256(body)
	return hex.EncodeToString(value[:])
}
