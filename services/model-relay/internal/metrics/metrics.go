// Package metrics declares the Prometheus metric families the relay exposes
// on GET /metrics. Families mirror apps/platform/ditto/metrics.py so relay
// dashboards keep their series names across the Python→Go cutover.
//
// The one family the relay request paths actually increment is
// AdmissionAtCapacity; the validator/janitor families are platform-path in
// Python and exist here only as declared (childless) vecs for exposition
// parity. Labeled vecs with no children emit only HELP/TYPE lines, exactly
// like the Python client.
package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// Admission lane label values. NOTE: the metric lane vocabulary is
// {chat, embedding} — distinct from the wire decline lane {inference, embedding}.
const (
	LaneChat      = "chat"
	LaneEmbedding = "embedding"
)

// Admission scope label values, named for the FIRST (narrowest) gate that
// bound.
const (
	ScopeTokenReservation = "token_reservation"
	ScopePerTicket        = "per_ticket"
	ScopePerValidator     = "per_validator"
	ScopeGlobal           = "global"
	ScopePerTicketRPM     = "per_ticket_rpm"
	ScopePerValidatorRPM  = "per_validator_rpm"
	ScopeGlobalRPM        = "global_rpm"
)

// AdmissionAtCapacity is ditto_inference_admission_at_capacity_total{lane,scope}:
// incremented inside admission when the retryable AT_CAPACITY decline fires,
// labeled with the narrowest gate that bound.
var AdmissionAtCapacity = promauto.NewCounterVec(prometheus.CounterOpts{
	Name: "ditto_inference_admission_at_capacity_total",
	Help: "Inference admissions declined with the retryable AT_CAPACITY decline, by lane and first-bound scope.",
}, []string{"lane", "scope"})

// Platform-path families, declared for exposition parity (static on a relay).
var (
	ValidatorHeartbeatPayloadDegraded = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ditto_validator_heartbeat_payload_degraded_total",
		Help: "Validator heartbeat payloads degraded during parsing (platform role; static on a relay).",
	}, []string{"stage", "reason"})

	ValidatorDispatchDeclined = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ditto_validator_dispatch_declined_total",
		Help: "Validator dispatches declined (platform role; static on a relay).",
	}, []string{"reason"})

	NonceJanitorRuns = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ditto_validator_nonce_janitor_runs_total",
		Help: "Validator nonce janitor sweeps (platform role; static on a relay).",
	}, []string{"outcome"})

	NonceJanitorDeleted = promauto.NewCounter(prometheus.CounterOpts{
		Name: "ditto_validator_nonce_janitor_deleted_total",
		Help: "Expired validator nonces deleted by the janitor (platform role; static on a relay).",
	})

	NonceJanitorDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name: "ditto_validator_nonce_janitor_duration_seconds",
		Help: "Validator nonce janitor sweep duration (platform role; static on a relay).",
	})

	LeaseForceExpiry = promauto.NewCounter(prometheus.CounterOpts{
		Name: "ditto_lease_force_expiry_total",
		Help: "Validator lease force-expiries (platform role; static on a relay).",
	})

	LeaseForceExpiryDeclined = promauto.NewCounter(prometheus.CounterOpts{
		Name: "ditto_lease_force_expiry_declined_total",
		Help: "Validator lease force-expiries declined (platform role; static on a relay).",
	})
)
