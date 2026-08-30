// Package codingsupervisor defines the authenticated private control-plane
// boundary between the Python shadow-attempt coordinator and a future trusted
// Go attempt backend.
//
// The package owns strict bounded wire validation, per-attempt single flight,
// cancellation, and safe error projection. It does not mount itself, construct
// a backend, start a harness, claim work, call Platform, score, or set weights.
package codingsupervisor
