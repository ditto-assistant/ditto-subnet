package chain

// CheckExtrinsicSuccess is the Pylon-gap helper that resolves
// “system.ExtrinsicSuccess“ / “ExtrinsicFailed“ events at a known
// block hash. The Python client implements this via
// async-substrate-interface; the Go side does not yet ship a SCALE
// decoder, so this method returns ErrNotImplemented.
//
// Validators that need the success bit today have three options:
//
//   - Trust Pylon's retry semantics for put_weights and skip the check.
//   - Shell out to "python -m ditto.chain.cli check-extrinsic" (the
//     Python ChainClient is the source of truth for the substrate gap
//     until the Go decoder lands).
//   - Wait for the follow-up plan that adds a Go SCALE codec and a
//     pluggable substrate transport.
//
// The signature is fixed now so callers can wire against it; replacing
// the body with a real implementation in a follow-up will not break
// downstream callers.
func (c *Client) CheckExtrinsicSuccess(blockHash string, extrinsicIndex int) (bool, error) {
	_ = blockHash
	_ = extrinsicIndex
	return false, ErrNotImplemented
}
