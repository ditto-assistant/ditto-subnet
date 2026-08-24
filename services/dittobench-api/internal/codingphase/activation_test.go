package codingphase

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codingsupervisor"
)

type activationRoundTrip func(*http.Request) (*http.Response, error)

func (function activationRoundTrip) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestJournalDirectoryCapacityIsDurableAndFailClosed(t *testing.T) {
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	first := "relay-" + strings.Repeat("a", 64)
	second := "relay-" + strings.Repeat("b", 64)
	if err := ensureBoundedJournalDirectory(root, first, 1); err != nil {
		t.Fatal(err)
	}
	if err := ensureBoundedJournalDirectory(root, first, 1); err != nil {
		t.Fatalf("exact directory replay: %v", err)
	}
	if err := ensureBoundedJournalDirectory(root, second, 1); err == nil {
		t.Fatal("journal directory capacity was bypassed")
	}
	if err := os.WriteFile(root+"/unexpected", []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := ensureBoundedJournalDirectory(root, first, 2); err == nil {
		t.Fatal("unexpected relay-root entry was accepted")
	}
}

type activationAuthorizer struct{ calls int }

func (authorizer *activationAuthorizer) Authorize(context.Context, codinggateway.CapabilityBinding) error {
	authorizer.calls++
	return nil
}

type activationPublisher struct {
	calls  int
	handle *activationCapability
}

func (publisher *activationPublisher) Publish(
	_ context.Context,
	_ codinggateway.CapabilityBinding,
	_ http.Handler,
) (codinggateway.PublishedCapability, error) {
	publisher.calls++
	publisher.handle = &activationCapability{url: "http://host.docker.internal:11437/v1/coding/inference/synthetic"}
	return publisher.handle, nil
}

type activationCapability struct {
	url     string
	revoked bool
	closed  bool
}

func (capability *activationCapability) URL() string { return capability.url }
func (capability *activationCapability) Revoke(context.Context) error {
	capability.revoked = true
	capability.url = ""
	return nil
}
func (capability *activationCapability) Close() error {
	if !capability.revoked {
		return errors.New("route not revoked")
	}
	capability.closed = true
	return nil
}

func parsedActivationFixture(t *testing.T) (*phaseFixture, parsedGrant) {
	t.Helper()
	fixture := newPhaseFixture(t)
	authority, err := parseAuthoringAuthority(fixture.input.Request, fixture.policy, fixture.now)
	if err != nil {
		t.Fatal(err)
	}
	grant, err := parseGrant(
		fixture.input.Request.Grant, fixture.input.Request, authority, fixture.policy,
		"harness-phase-001", fixture.input.SessionID, fixture.input.BrokerPublicKey,
		fixture.input.BrokerPrivateKey, fixture.now,
	)
	if err != nil {
		t.Fatal(err)
	}
	return fixture, grant
}

func TestGatewayActivatorCreatesOneJournalAndRevokesExactGrant(t *testing.T) {
	fixture, grant := parsedActivationFixture(t)
	publisher := &activationPublisher{}
	revokeCalls := 0
	transport := activationRoundTrip(func(request *http.Request) (*http.Response, error) {
		if request.URL.Path != "/api/v1/validator/coding-shadow/inference-revoke-capability" ||
			request.Header.Get("Authorization") != "Bearer "+grant.revocation.Bearer {
			t.Fatalf("revoke request drift: %s", request.URL)
		}
		revokeCalls++
		body := `{"schema":"dittobench-coding-inference-revocation-v1","coding_contract_version":1,"weight_eligible":false,"grant_id":"` +
			grant.revocation.GrantID + `","ticket_id":"` + grant.revocation.TicketID +
			`","status":"revoked","generation":1,"revoked_at":"` + fixture.now.Format(time.RFC3339) + `","idempotent":false}`
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}, "Cache-Control": {"no-store"}},
			Body:       io.NopCloser(strings.NewReader(body)),
		}, nil
	})
	journalRoot := t.TempDir()
	if err := os.Chmod(journalRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	activator, err := NewGatewayActivator(GatewayActivatorConfig{
		JournalRoot: journalRoot, JournalMaxTotalBytes: 1 << 30, JournalMaxEntries: 256,
		Publisher: publisher, Transport: transport, Now: func() time.Time { return fixture.now },
		NewRequestID: func() string { return "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" },
		NewNonce:     func() string { return "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" },
	})
	if err != nil {
		t.Fatal(err)
	}
	authorizer := &activationAuthorizer{}
	gateway, err := activator.Activate(t.Context(), InferenceActivation{
		Policy: fixture.policy, Capability: grant.capability,
		Revocation: grant.revocation, Authorizer: authorizer,
	})
	if err != nil {
		t.Fatal(err)
	}
	if authorizer.calls != 1 || publisher.calls != 1 {
		t.Fatalf("authorizer/publisher=%d/%d", authorizer.calls, publisher.calls)
	}
	if _, err := gateway.URL(); err != nil {
		t.Fatal(err)
	}
	if err := gateway.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if revokeCalls != 1 || !publisher.handle.revoked {
		t.Fatalf("revoke calls/route=%d/%v", revokeCalls, publisher.handle.revoked)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}
	if !publisher.handle.closed {
		t.Fatal("published route did not close")
	}

	_, err = activator.Activate(t.Context(), InferenceActivation{
		Policy: fixture.policy, Capability: grant.capability,
		Revocation: grant.revocation, Authorizer: authorizer,
	})
	if !errors.Is(err, codinggateway.ErrAlreadyUsed) {
		t.Fatalf("journal replay err=%v", err)
	}
	if revokeCalls != 2 || publisher.calls != 1 {
		t.Fatalf("replay revoke/publish=%d/%d", revokeCalls, publisher.calls)
	}
}

func TestGatewayActivatorRejectsInvalidPrivateConfiguration(t *testing.T) {
	for name, config := range map[string]GatewayActivatorConfig{
		"relative root": {JournalRoot: "relative", JournalMaxTotalBytes: 1, JournalMaxEntries: 1, Publisher: &activationPublisher{}},
		"root":          {JournalRoot: "/", JournalMaxTotalBytes: 1, JournalMaxEntries: 1, Publisher: &activationPublisher{}},
		"capacity":      {JournalRoot: t.TempDir(), JournalMaxTotalBytes: 0, JournalMaxEntries: 1, Publisher: &activationPublisher{}},
		"entries":       {JournalRoot: t.TempDir(), JournalMaxTotalBytes: 1, JournalMaxEntries: 0, Publisher: &activationPublisher{}},
		"publisher":     {JournalRoot: t.TempDir(), JournalMaxTotalBytes: 1, JournalMaxEntries: 1},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := NewGatewayActivator(config); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("err=%v", err)
			}
		})
	}
	activator := &GatewayActivator{}
	if _, err := json.Marshal(activator); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("marshal err=%v", err)
	}
}

func TestGatewayActivatorRevokesBeforeRejectingInsufficientJournalCapacity(t *testing.T) {
	fixture, grant := parsedActivationFixture(t)
	revokeCalls := 0
	transport := activationRoundTrip(func(*http.Request) (*http.Response, error) {
		revokeCalls++
		body := `{"schema":"dittobench-coding-inference-revocation-v1","coding_contract_version":1,"weight_eligible":false,"grant_id":"` +
			grant.revocation.GrantID + `","ticket_id":"` + grant.revocation.TicketID +
			`","status":"revoked","generation":1,"revoked_at":"` + fixture.now.Format(time.RFC3339) + `","idempotent":false}`
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}, "Cache-Control": {"no-store"}},
			Body:       io.NopCloser(strings.NewReader(body)),
		}, nil
	})
	publisher := &activationPublisher{}
	journalRoot := t.TempDir()
	if err := os.Chmod(journalRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	activator, err := NewGatewayActivator(GatewayActivatorConfig{
		JournalRoot: journalRoot, JournalMaxTotalBytes: 1 << 30, JournalMaxEntries: 1,
		Publisher: publisher, Transport: transport, Now: func() time.Time { return fixture.now },
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = activator.Activate(t.Context(), InferenceActivation{
		Policy: fixture.policy, Capability: grant.capability,
		Revocation: grant.revocation, Authorizer: &activationAuthorizer{},
	})
	if !errors.Is(err, ErrLifecycle) || revokeCalls != 1 || publisher.calls != 0 {
		t.Fatalf("err/revoke/publish=%v/%d/%d", err, revokeCalls, publisher.calls)
	}
}

func TestGatewayActivatorRevokesWhenActivationContextIsCanceled(t *testing.T) {
	fixture, grant := parsedActivationFixture(t)
	revokeCalls := 0
	transport := activationRoundTrip(func(*http.Request) (*http.Response, error) {
		revokeCalls++
		body := `{"schema":"dittobench-coding-inference-revocation-v1","coding_contract_version":1,"weight_eligible":false,"grant_id":"` +
			grant.revocation.GrantID + `","ticket_id":"` + grant.revocation.TicketID +
			`","status":"revoked","generation":1,"revoked_at":"` + fixture.now.Format(time.RFC3339) + `","idempotent":false}`
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}, "Cache-Control": {"no-store"}},
			Body:       io.NopCloser(strings.NewReader(body)),
		}, nil
	})
	journalRoot := t.TempDir()
	if err := os.Chmod(journalRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	activator, err := NewGatewayActivator(GatewayActivatorConfig{
		JournalRoot: journalRoot, JournalMaxTotalBytes: 1 << 30, JournalMaxEntries: 8,
		Publisher: &activationPublisher{}, Transport: transport, Now: func() time.Time { return fixture.now },
	})
	if err != nil {
		t.Fatal(err)
	}
	canceled, cancel := context.WithCancel(t.Context())
	cancel()
	_, err = activator.Activate(canceled, InferenceActivation{
		Policy: fixture.policy, Capability: grant.capability,
		Revocation: grant.revocation, Authorizer: &activationAuthorizer{},
	})
	if !errors.Is(err, ErrLifecycle) || revokeCalls != 1 {
		t.Fatalf("err/revoke=%v/%d", err, revokeCalls)
	}
}

var _ codingsupervisor.PhaseRunner = (*Runner)(nil)
