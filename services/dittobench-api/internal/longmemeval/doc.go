// Package longmemeval defines the pure, content-addressed core of the bounded
// DittoBench v9 LongMemEval confirmation dimension.
//
// The package freezes and validates profiles, loads and projects pinned dataset
// cases, executes them through bounded harness, metering, and judge interfaces,
// aggregates judged outcomes, validates authoritative provider accounting, and
// hashes canonical evidence. Scheduler, signer, persistence, and provider-
// credential integration remain outside this package.
package longmemeval
