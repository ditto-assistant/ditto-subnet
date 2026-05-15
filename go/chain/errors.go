// Package chain provides a Go client for the Ditto Bittensor subnet's
// chain access layer. It mirrors the Python implementation in
// ditto/chain/ (Pylon HTTP + a substrate gap-filler) so a Go validator
// binary can perform the same neuron lookups, block reads, and weight
// commits that the Python services already do.
//
// Wire-shape disclaimer: Pylon is a heyditto-internal service whose
// official client is Python (pylon_client). The Go client here speaks
// HTTP/JSON against the same endpoints the Python SDK calls under the
// hood; the request and response shapes are defined here in models.go.
// When a Pylon REST-shape change lands in the Python SDK, mirror it
// here so the two clients stay in lockstep.
package chain

import "errors"

// ErrChainConnection signals a connection or authentication failure
// against Pylon or its underlying subtensor node. Wrap it with %w when
// surfacing transport errors so callers can errors.Is() on it.
var ErrChainConnection = errors.New("chain: connection error")

// ErrChainTimeout signals that a chain request exceeded its budget. Wrap
// with %w when the underlying transport returned a context.DeadlineExceeded
// or net.Error.Timeout(); errors.Is(err, ErrChainTimeout) lets callers
// classify the failure without unwrapping transport details.
var ErrChainTimeout = errors.New("chain: timeout")

// ErrExtrinsicNotFound signals that no extrinsic exists at the requested
// (block, index) pair, or that no ExtrinsicSuccess/Failed event was found
// when calling CheckExtrinsicSuccess.
var ErrExtrinsicNotFound = errors.New("chain: extrinsic not found")

// ErrNotImplemented is returned for the substrate-gap helpers
// (CheckExtrinsicSuccess) until a Go SCALE decoder lands. Validators
// that need the event-status check can either fall back to the Python
// implementation in ditto/chain/client.py or wait for the follow-up plan
// that adds a Go SCALE codec.
var ErrNotImplemented = errors.New("chain: not implemented in Go yet")
