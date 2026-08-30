// Package codingrelayjournal provides the validator-local durable filesystem
// implementation of codingrelay.Journal.
//
// One Store owns one relay root. The root is a private directory capability,
// every dispatch is durable before provider activity, completion replaces only
// its matching dispatch record, and revocation is an independent durable state
// transition. The package does not open a listener, contact Platform or a
// provider, retain credentials, publish evidence, score a patch, or activate
// coding weights.
package codingrelayjournal
