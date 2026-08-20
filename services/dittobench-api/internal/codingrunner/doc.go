// Package codingrunner implements the shadow-only DittoBench Coding authoring
// workspace and authoritative freezer.
//
// The package is deliberately not wired into the production scorer. A caller
// must supply one already-authorized, task-scoped manifest and visible capsule,
// mount the returned HTTP handler behind a source-bound capability, and inject
// a trusted command executor. The package never starts candidate processes on
// the scorer host by itself.
package codingrunner
