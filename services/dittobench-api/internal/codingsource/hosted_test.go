package codingsource

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
)

func TestHostedRoutesBindFullAuthorityAndRejectLegacyAndSpoofedSources(t *testing.T) {
	f := newRouterFixture(t)
	binding := HostedBinding{
		HarnessInstanceID: "hosted-instance", AgentArtifactSHA256: strings.Repeat("d", 64),
		EvaluationID: "10000000-0000-4000-8000-000000000001", AttemptID: "20000000-0000-4000-8000-000000000002",
		WorkerID: "30000000-0000-4000-8000-000000000003", AssignmentSHA256: strings.Repeat("a", 64),
		ProfileCapabilityID: "hosted-profile", Deadline: f.now.Add(time.Hour),
	}
	lease, err := f.registry.RegisterHosted(binding, "172.30.0.8")
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusNoContent) })
	legacy := codingcertifier.CapabilityBinding{HarnessInstanceID: binding.HarnessInstanceID, AgentArtifactSHA256: binding.AgentArtifactSHA256, TicketID: binding.EvaluationID, CaseID: binding.AttemptID, ProfileCapabilityID: binding.ProfileCapabilityID}
	if _, err := f.router.WorkspacePublisher().Publish(t.Context(), legacy, handler); err == nil {
		t.Fatal("legacy resolved native source")
	}
	for _, mutate := range []func(*HostedBinding){
		func(b *HostedBinding) { b.AssignmentSHA256 = strings.Repeat("b", 64) },
		func(b *HostedBinding) { b.WorkerID = b.EvaluationID },
		func(b *HostedBinding) { b.AttemptID = b.EvaluationID },
		func(b *HostedBinding) { b.Deadline = b.Deadline.Add(-time.Second) },
	} {
		wrong := binding
		mutate(&wrong)
		if _, err := f.router.PublishHostedWorkspace(t.Context(), wrong, handler); err == nil {
			t.Fatal("authority drift accepted")
		}
	}
	workspace, err := f.router.PublishHostedWorkspace(t.Context(), binding, handler)
	if err != nil {
		t.Fatal(err)
	}
	inference, err := f.router.PublishHostedInference(t.Context(), binding, handler)
	if err != nil {
		t.Fatal(err)
	}
	for _, route := range []codingcertifier.PublishedCapability{workspace, inference} {
		if !strings.Contains(route.URL(), "/v2/coding/") {
			t.Fatal("not native route")
		}
		for _, remote := range []string{"172.30.0.8:1234", "172.30.0.7:1234", "127.0.0.1:1234"} {
			r := f.request(t, route.URL(), remote)
			r.Header.Set("X-Forwarded-For", "172.30.0.8")
			w := httptest.NewRecorder()
			f.router.ServeHTTP(w, r)
			want := http.StatusNotFound
			if remote == "172.30.0.8:1234" {
				want = http.StatusNoContent
			}
			if w.Code != want {
				t.Fatalf("source status = %d, want %d", w.Code, want)
			}
		}
	}
	oldURL := workspace.URL()
	if err := lease.Close(); err != nil {
		t.Fatal(err)
	}
	replacement, err := f.registry.RegisterHosted(binding, "172.30.0.8")
	if err != nil {
		t.Fatal(err)
	}
	defer replacement.Close()
	w := httptest.NewRecorder()
	f.router.ServeHTTP(w, f.request(t, oldURL, "172.30.0.8:1234"))
	if w.Code != http.StatusNotFound {
		t.Fatal("old route followed replacement registration")
	}
	for _, route := range []codingcertifier.PublishedCapability{workspace, inference} {
		if err := route.Revoke(context.Background()); err != nil {
			t.Fatal(err)
		}
		if err := route.Close(); err != nil {
			t.Fatal(err)
		}
	}
}
