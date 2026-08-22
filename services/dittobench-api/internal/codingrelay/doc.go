// Package codingrelay implements the unwired, ticket-bound DittoBench Coding
// Luna relay core.
//
// The package owns request locking, durable dispatch ordering, bounded retries,
// response projection, revocation, and deterministic model evidence. It does
// not own a listener, provider credential, Platform grant exchange, harness
// lifecycle, score, or weight path. A future validator-local gateway must mount
// Handler behind a source-bound capability and provide durable Journal and
// trusted Upstream implementations.
package codingrelay
