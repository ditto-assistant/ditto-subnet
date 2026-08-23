// Package codingsupervisor defines the authenticated private control-plane
// boundary between the Python shadow-attempt coordinator and a future trusted
// Go attempt backend. Its process-local SessionBackend owns prepared broker
// keys and no-clean-retry phase ordering while delegating all phase work.
//
// The package owns strict bounded wire validation, per-attempt single flight,
// cancellation, and safe error projection. It does not mount itself, construct
// a phase runner, start a harness, claim work, call Platform, score, or set
// weights.
package codingsupervisor
