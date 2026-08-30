// Package codingcanary is the authenticated control-plane boundary that runs
// one public certification canary after a validator claims a qualified lease.
//
// Construction is default-off at the command boundary. The backend loads the
// pinned public pack, acquires a lease-shaped screened harness, runs
// codingcertifier, and always revokes capabilities and destroys the harness.
// It does not claim Platform work, score, or set weights.
package codingcanary
