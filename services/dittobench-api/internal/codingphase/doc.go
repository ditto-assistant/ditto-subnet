// Package codingphase owns the synchronous, fail-closed composition of one
// shadow coding authoring phase and its later pristine grading phase.
//
// Authoring commits the evidence-outbox collecting marker before any harness
// health, seed, run, workspace route, or inference route is exposed. Every
// terminal path revokes capabilities, freezes or expires the attempt, destroys
// the harness, and never grants a clean retry after candidate activity.
//
// The package deliberately does not publish Platform requests or activate a
// worker. A later disabled wiring layer supplies the concrete harness and
// inference factories and owns exact publication envelopes.
package codingphase
