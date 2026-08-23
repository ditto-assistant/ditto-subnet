package codingsource

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

func fixtureSourceBinding(now time.Time, suffix string) HarnessBinding {
	return HarnessBinding{
		HarnessInstanceID:   "coding-harness-" + suffix,
		AgentArtifactSHA256: strings.Repeat("a", 64),
		TicketID:            "33333333-3333-4333-8333-333333333333",
		CaseID:              "case-" + suffix, ProfileCapabilityID: "profile-" + suffix,
		Deadline: now.Add(time.Hour),
	}
}

func TestRegistryOwnsExactInstanceAndPrivateSource(t *testing.T) {
	now := time.Date(2026, 8, 23, 18, 0, 0, 0, time.UTC)
	registry := NewRegistry(func() time.Time { return now })
	binding := fixtureSourceBinding(now, "one")
	lease, err := registry.Register(binding, "172.20.0.2")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Register(binding, "172.20.0.3"); !errors.Is(err, ErrConflict) {
		t.Fatalf("duplicate instance err=%v", err)
	}
	other := fixtureSourceBinding(now, "two")
	if _, err := registry.Register(other, "172.20.0.2"); !errors.Is(err, ErrConflict) {
		t.Fatalf("duplicate source err=%v", err)
	}
	for _, source := range []string{"127.0.0.1", "0.0.0.0", "203.0.113.9", "not-an-ip"} {
		if _, err := registry.Register(fixtureSourceBinding(now, source), source); !errors.Is(err, ErrInvalid) {
			t.Fatalf("source=%q err=%v", source, err)
		}
	}
	if err := lease.Close(); err != nil {
		t.Fatal(err)
	}
	if err := lease.Close(); err != nil {
		t.Fatal(err)
	}
	if _, ok := registry.resolve(
		binding.HarnessInstanceID, binding.AgentArtifactSHA256, binding.TicketID,
		binding.CaseID, binding.ProfileCapabilityID, &binding.Deadline,
	); ok {
		t.Fatal("closed source registration still resolved")
	}
}

func TestRegistryFailsClosedOnExpiryAndClockRollback(t *testing.T) {
	now := time.Date(2026, 8, 23, 18, 0, 0, 0, time.UTC)
	clock := now
	registry := NewRegistry(func() time.Time { return clock })
	binding := fixtureSourceBinding(now, "clock")
	lease, err := registry.Register(binding, "10.9.0.3")
	if err != nil {
		t.Fatal(err)
	}
	record, ok := registry.resolve(
		binding.HarnessInstanceID, binding.AgentArtifactSHA256, binding.TicketID,
		binding.CaseID, binding.ProfileCapabilityID, nil,
	)
	if !ok {
		t.Fatal("active source did not resolve")
	}
	clock = now.Add(30 * time.Minute)
	if !registry.matches(record, record.address) {
		t.Fatal("active source did not match")
	}
	clock = now.Add(29 * time.Minute)
	if registry.matches(record, record.address) {
		t.Fatal("clock rollback reopened source route")
	}
	clock = binding.Deadline
	if _, ok := registry.resolve(
		binding.HarnessInstanceID, binding.AgentArtifactSHA256, binding.TicketID,
		binding.CaseID, binding.ProfileCapabilityID, nil,
	); ok {
		t.Fatal("expired source route resolved")
	}
	if err := lease.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestSourcePrivateStateRejectsJSONAndDiagnostics(t *testing.T) {
	now := time.Now().UTC()
	registry := NewRegistry(func() time.Time { return now })
	lease, err := registry.Register(fixtureSourceBinding(now, "private"), "192.168.50.4")
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]any{
		"binding": fixtureSourceBinding(now, "private"), "registry": registry, "lease": lease,
	} {
		if _, err := json.Marshal(value); !errors.Is(err, ErrInvalid) {
			t.Fatalf("%s marshal err=%v", name, err)
		}
	}
	for _, output := range []string{registry.String(), lease.String()} {
		if strings.Contains(output, "192.168") || strings.Contains(output, "33333333") {
			t.Fatalf("private identity leaked: %q", output)
		}
	}
	if err := lease.Close(); err != nil {
		t.Fatal(err)
	}
}
