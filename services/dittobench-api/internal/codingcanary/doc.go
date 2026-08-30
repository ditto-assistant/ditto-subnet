// Package codingcanary is the authenticated control-plane boundary that runs
// one public certification canary after a validator claims a qualified lease.
//
// Construction is default-off at the command boundary. The backend loads the
// pinned public pack, acquires a lease-shaped screened harness, activates the
// existing source-bound inference gateway from an exchanged claimed-lease
// grant, runs codingcertifier against that relay, and always revokes the
// gateway and destroys the harness. Unused inference still yields a persistable
// failed receipt. It does not claim Platform work, score, or set weights.
package codingcanary
