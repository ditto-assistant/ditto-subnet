package routerharness

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// HealthPath is the endpoint every submission MAY serve to advertise a router
// project. It mirrors the coding track's /coding/health probe and the reference
// starter kit's GET /router/health.
const HealthPath = "/router/health"

// RouterContractVersion is the router-track contract this scorer speaks. It
// matches routerscore.RouterContractVersion and the Rust starter kit's
// ROUTER_CONTRACT_VERSION; a submission must advertise support for it to be
// included.
const RouterContractVersion = 1

// InclusionStatus is the benign, yes-and classification of a submission's router
// participation. It is never a deny: a submission without a router project is
// StatusUnsupported and is scored on the memory contract exactly as before.
type InclusionStatus string

const (
	// StatusSupported: the submission advertised a valid router project and the
	// additive router run may proceed.
	StatusSupported InclusionStatus = "supported"
	// StatusUnsupported: no router project (404), unreachable, or a malformed /
	// incompatible advertisement. Benign — the router run is skipped, no penalty,
	// no router score. This is the "only run if the submission includes the
	// router project" gate, done the backwards-compatible way.
	StatusUnsupported InclusionStatus = "unsupported"
)

// HealthAdvertisement is the shape the starter kit serves at /router/health. The
// JSON keys match miners/dittobench-router-starter-kit/src/protocol.rs exactly.
type HealthAdvertisement struct {
	Status                          string   `json:"status"`
	SupportedRouterContractVersions []uint32 `json:"supported_router_contract_versions"`
	Wires                           []string `json:"wires"`
	CountTokens                     bool     `json:"count_tokens"`
}

// InclusionResult is the typed outcome of the opt-in probe. Included mirrors
// Status == StatusSupported; Reason is a short human-readable note for
// telemetry (never surfaced to the miner as a penalty).
type InclusionResult struct {
	Included bool            `json:"included"`
	Status   InclusionStatus `json:"status"`
	Contract int             `json:"contract"`
	Wires    []string        `json:"wires"`
	Reason   string          `json:"reason"`
}

// unsupported builds a benign skip result. The router run does not run; the
// submission is otherwise scored unchanged.
func unsupported(reason string) InclusionResult {
	return InclusionResult{Included: false, Status: StatusUnsupported, Reason: reason}
}

// httpGetFn is the injected HTTP getter, so ProbeInclusion is unit-testable
// without a live server. It has the shape of (*http.Client).Get.
type httpGetFn func(url string) (*http.Response, error)

// maxHealthBody caps the advertisement read so a hostile submission cannot make
// the scorer buffer an unbounded body during the opt-in probe.
const maxHealthBody = 1 << 16

// ProbeInclusion performs the opt-in ("yes-and") router-project gate: it GETs
// routerBaseURL+HealthPath and classifies the submission.
//
//   - A valid advertisement (HTTP 200, RouterContractVersion supported, non-empty
//     wires) -> StatusSupported: the additive router run may proceed.
//   - 404, any non-200, an unreachable server, a malformed body, or an
//     advertisement that does not support the contract / lists no wires ->
//     StatusUnsupported: a benign skip, NEVER a deny. The submission's memory
//     scoring is untouched.
//
// This is the single place the gate lives; every RouterBackend implementation
// dispatches only after it returns Included, so all compute destinations share
// identical backwards-compatible semantics.
func ProbeInclusion(ctx context.Context, routerBaseURL string, get httpGetFn) InclusionResult {
	base := strings.TrimRight(strings.TrimSpace(routerBaseURL), "/")
	if base == "" {
		return unsupported("empty router base url")
	}
	if get == nil {
		get = http.DefaultClient.Get
	}
	// ctx is honored by callers that inject a context-aware getter; the default
	// client getter has no ctx, so we check cancellation up front too.
	if err := ctx.Err(); err != nil {
		return unsupported(fmt.Sprintf("context: %v", err))
	}

	resp, err := get(base + HealthPath)
	if err != nil {
		return unsupported(fmt.Sprintf("no router project: %v", err))
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		// The canonical "no router project" signal: benign, additive-only.
		return unsupported("no router project (404)")
	}
	if resp.StatusCode != http.StatusOK {
		return unsupported(fmt.Sprintf("health status %d", resp.StatusCode))
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxHealthBody))
	if err != nil {
		return unsupported(fmt.Sprintf("read health body: %v", err))
	}
	var ad HealthAdvertisement
	if err := json.Unmarshal(body, &ad); err != nil {
		return unsupported("malformed health advertisement")
	}

	if !supportsContract(ad.SupportedRouterContractVersions, RouterContractVersion) {
		return unsupported(fmt.Sprintf(
			"contract %d not in advertised %v",
			RouterContractVersion, ad.SupportedRouterContractVersions,
		))
	}
	if len(ad.Wires) == 0 {
		return unsupported("advertisement lists no wires")
	}

	return InclusionResult{
		Included: true,
		Status:   StatusSupported,
		Contract: RouterContractVersion,
		Wires:    append([]string(nil), ad.Wires...),
		Reason:   "router project advertised",
	}
}

func supportsContract(advertised []uint32, want int) bool {
	for _, v := range advertised {
		if int(v) == want {
			return true
		}
	}
	return false
}
