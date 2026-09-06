package codingcertifier

import (
	"context"
	"errors"
	"net/http"
	"reflect"
	"slices"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

// HostedHTTPHarnessClient is a Platform-owned v2 client. Its distinct method
// types prevent accidental use in the v1 certifier. Health is compatibility
// information, never proof of a completed private evaluation.
type HostedHTTPHarnessClient struct{ inner *HTTPHarnessClient }

func NewHostedHTTPHarnessClient(baseURL string, client *http.Client) (*HostedHTTPHarnessClient, error) {
	if client == nil || client.Transport == nil {
		return nil, errors.New("hosted harness requires an explicit authorized transport")
	}
	value := reflect.ValueOf(client.Transport)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		if value.IsNil() {
			return nil, errors.New("hosted harness transport is unavailable")
		}
	}
	if transport, ok := client.Transport.(*http.Transport); ok && transport.Proxy != nil {
		return nil, errors.New("hosted harness transport must not use an ambient proxy")
	}
	inner, err := NewHTTPHarnessClient(baseURL, client)
	if err != nil {
		return nil, err
	}
	return &HostedHTTPHarnessClient{inner: inner}, nil
}

func (health HealthResponse) SupportsCodingV2() bool {
	if health.validate() != nil || !slices.Contains(health.SupportedCodingContractVersions, codingcontract.HostedHarnessVersion) {
		return false
	}
	for _, capability := range []string{"scoped_memory_seed_v2", "coding_runner_tools_v2", "case_scoped_inference_v2"} {
		if !slices.Contains(health.Capabilities, capability) {
			return false
		}
	}
	return true
}

func (client *HostedHTTPHarnessClient) Health(ctx context.Context) (HealthResponse, error) {
	if client == nil || client.inner == nil || ctx == nil {
		return HealthResponse{}, errors.New("hosted harness client is unavailable")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	var response HealthResponse
	status, err := client.inner.do(ctx, http.MethodGet, "/coding/health", nil, maxHealthResponseBytes, &response)
	if status == http.StatusNotFound || status == http.StatusMethodNotAllowed {
		return HealthResponse{}, ErrCodingUnsupported
	}
	if err != nil {
		return HealthResponse{}, err
	}
	if err := ctx.Err(); err != nil {
		return HealthResponse{}, err
	}
	if status != http.StatusOK {
		return HealthResponse{}, harnessFailure("health", HarnessFailureProtocol, status, errors.New("unexpected status"))
	}
	if !response.SupportsCodingV2() {
		return HealthResponse{}, ErrCodingUnsupported
	}
	return response.normalized(), nil
}

func (client *HostedHTTPHarnessClient) Seed(ctx context.Context, request codingcontract.HostedSeedRequest) (SeedResponse, error) {
	if client == nil || client.inner == nil || ctx == nil {
		return SeedResponse{}, errors.New("hosted harness client is unavailable")
	}
	if err := request.Validate(); err != nil {
		return SeedResponse{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	var wire struct {
		CaseID              string `json:"case_id"`
		ProfileCapabilityID string `json:"profile_capability_id"`
		MemoryBundleSHA256  string `json:"memory_bundle_sha256"`
		MemoryCount         *int   `json:"memory_count"`
		IdempotentReplay    *bool  `json:"idempotent_replay"`
	}
	status, err := client.inner.do(ctx, http.MethodPost, "/coding/seed", request, maxSeedResponseBytes, &wire)
	if err != nil {
		return SeedResponse{}, err
	}
	if err := ctx.Err(); err != nil {
		return SeedResponse{}, err
	}
	if status != http.StatusOK || wire.MemoryCount == nil || wire.IdempotentReplay == nil {
		return SeedResponse{}, harnessFailure("seed", HarnessFailureProtocol, status, errors.New("incomplete acknowledgement"))
	}
	response := SeedResponse{CaseID: wire.CaseID, ProfileCapabilityID: wire.ProfileCapabilityID, MemoryBundleSHA256: wire.MemoryBundleSHA256, MemoryCount: *wire.MemoryCount, IdempotentReplay: *wire.IdempotentReplay}
	if err := response.ValidateIdentity(codingcontract.SeedRequest(request)); err != nil {
		return SeedResponse{}, harnessFailure("seed", HarnessFailureProtocol, http.StatusOK, err)
	}
	return response, nil
}

func (client *HostedHTTPHarnessClient) Run(ctx context.Context, request codingcontract.HostedRunRequest) (RunResponse, error) {
	if client == nil || client.inner == nil || ctx == nil {
		return RunResponse{}, errors.New("hosted harness client is unavailable")
	}
	if err := request.Validate(); err != nil {
		return RunResponse{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(request.Budgets.WallTimeSeconds)*time.Second)
	defer cancel()
	var response RunResponse
	status, err := client.inner.do(ctx, http.MethodPost, "/coding/run", request, maxRunResponseBytes, &response)
	if err != nil {
		return RunResponse{}, err
	}
	if err := ctx.Err(); err != nil {
		return RunResponse{}, err
	}
	if status != http.StatusOK {
		return RunResponse{}, harnessFailure("run", HarnessFailureProtocol, status, errors.New("unexpected status"))
	}
	if err := response.validate(codingcontract.RunRequest(request)); err != nil {
		return RunResponse{}, harnessFailure("run", HarnessFailureProtocol, http.StatusOK, err)
	}
	return response, nil
}
