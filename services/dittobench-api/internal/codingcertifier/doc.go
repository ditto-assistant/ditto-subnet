// Package codingcertifier implements shadow-only active capability
// certification for DittoBench Coding.
//
// A health response is only an advertisement. Certification additionally
// requires one artifact-bound memory seed, one canary run through a
// source-bound workspace capability, durable transcript and frozen-submission
// evidence, trusted inference receipts, an authoritative freeze, and a
// pristine grade. The package emits no production score and cannot affect
// weights.
package codingcertifier
