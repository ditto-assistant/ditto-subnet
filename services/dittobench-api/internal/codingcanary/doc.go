// Package codingcanary is the authenticated control-plane boundary that runs
// one public certification canary after a validator claims a qualified lease.
//
// Construction is default-off at the command boundary. The handler always
// reports that capabilities were revoked and the harness destroyed. It does
// not claim Platform work, score, or set weights.
package codingcanary
