// Package codinggrader implements the shadow-only pristine deterministic
// grader for DittoBench Coding contract v1.
//
// The package has no scorer route and no default process executor. It replays
// only authoritative codingrunner submissions into a fresh workspace, injects
// grader material afterward, and emits deterministic evidence from a trusted
// executor supplied by the later sandbox layer.
package codinggrader
