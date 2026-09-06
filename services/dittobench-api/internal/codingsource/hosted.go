package codingsource

import (
	"context"
	"log/slog"
	"net/http"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
)

const (
	hostedWorkspacePrefix = "/v2/coding/workspace/"
	hostedInferencePrefix = "/v2/coding/inference/"
)

// HostedBinding is Platform-owned source identity, not a v1 validator ticket.
// AssignmentSHA256 must come from the committed assignment, never the harness.
type HostedBinding struct {
	HarnessInstanceID, AgentArtifactSHA256 string
	EvaluationID, AttemptID, WorkerID      string
	AssignmentSHA256, ProfileCapabilityID  string
	Deadline                               time.Time
}

func (binding HostedBinding) valid(now time.Time) bool {
	return validIdentifier(binding.HarnessInstanceID, 256) && lowerSHA256(binding.AgentArtifactSHA256) &&
		canonicalUUID(binding.EvaluationID) && canonicalUUID(binding.AttemptID) && canonicalUUID(binding.WorkerID) &&
		lowerSHA256(binding.AssignmentSHA256) && validIdentifier(binding.ProfileCapabilityID, 256) &&
		!now.IsZero() && binding.Deadline.After(now) && !binding.Deadline.After(now.Add(time.Hour))
}

func (registry *Registry) RegisterHosted(binding HostedBinding, sourceIP string) (*Lease, error) {
	if registry == nil || !binding.valid(registry.now().UTC()) {
		return nil, ErrInvalid
	}
	binding.Deadline = binding.Deadline.UTC()
	// Reuse only the version-neutral address/instance collision table. Legacy
	// resolution explicitly excludes hosted records, even with matching fields.
	return registry.register(HarnessBinding{
		HarnessInstanceID: binding.HarnessInstanceID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		TicketID: binding.EvaluationID, CaseID: binding.AttemptID,
		ProfileCapabilityID: binding.ProfileCapabilityID, Deadline: binding.Deadline,
	}, sourceIP, &binding)
}

func (registry *Registry) resolveHosted(binding HostedBinding) (*sourceRecord, bool) {
	if registry == nil {
		return nil, false
	}
	binding.Deadline = binding.Deadline.UTC()
	registry.mu.Lock()
	defer registry.mu.Unlock()
	now := registry.now().UTC()
	if now.Before(registry.lastNow) || !binding.valid(now) {
		return nil, false
	}
	registry.lastNow = now
	record := registry.byInstance[binding.HarnessInstanceID]
	return record, record != nil && record.hosted != nil && *record.hosted == binding
}

// PublishHostedWorkspace and PublishHostedInference only mount trusted handlers.
// They do not issue inference grants, authorize model policy, or open a dataset.
func (router *Router) PublishHostedWorkspace(ctx context.Context, binding HostedBinding, handler http.Handler) (codingcertifier.PublishedCapability, error) {
	return router.publishHosted(ctx, binding, handler, hostedWorkspacePrefix, "/tool")
}

func (router *Router) PublishHostedInference(ctx context.Context, binding HostedBinding, handler http.Handler) (codingcertifier.PublishedCapability, error) {
	return router.publishHosted(ctx, binding, handler, hostedInferencePrefix, "")
}

func (router *Router) publishHosted(ctx context.Context, binding HostedBinding, handler http.Handler, prefix, suffix string) (codingcertifier.PublishedCapability, error) {
	if router == nil || ctx == nil || ctx.Err() != nil || nilLike(handler) {
		return nil, ErrRoute
	}
	source, ok := router.registry.resolveHosted(binding)
	if !ok {
		return nil, ErrRoute
	}
	published, err := router.publish(prefix, suffix, source, handler)
	if err != nil {
		return nil, err
	}
	return published, nil
}

func (HostedBinding) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }
func (HostedBinding) String() string               { return "HostedSourceBinding{private}" }
func (value HostedBinding) GoString() string       { return value.String() }
func (value HostedBinding) LogValue() slog.Value   { return slog.StringValue(value.String()) }
