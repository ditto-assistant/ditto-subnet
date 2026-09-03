package codingcontract

import (
	schnorrkel "github.com/ChainSafe/go-schnorrkel"
	"github.com/mr-tron/base58"
	"golang.org/x/crypto/blake2b"
)

// VerifyExecutorSR25519 verifies a Bittensor hotkey signature for one
// executor-control signing message. It accepts the raw and substrate-interface
// Bytes-wrapped forms and returns false for every malformed input.
func VerifyExecutorSR25519(hotkey string, message, signature []byte) bool {
	publicKey, ok := executorSS58PublicKey(hotkey)
	if !ok || len(signature) != 64 {
		return false
	}
	public, err := schnorrkel.NewPublicKey(publicKey)
	if err != nil {
		return false
	}
	var encoded [64]byte
	copy(encoded[:], signature)
	value := new(schnorrkel.Signature)
	if err := value.Decode(encoded); err != nil {
		return false
	}
	verify := func(payload []byte) bool {
		valid, verifyErr := public.Verify(
			value,
			schnorrkel.NewSigningContext([]byte("substrate"), payload),
		)
		return verifyErr == nil && valid
	}
	if verify(message) {
		return true
	}
	wrapped := make([]byte, 0, len(message)+len("<Bytes></Bytes>"))
	wrapped = append(wrapped, "<Bytes>"...)
	wrapped = append(wrapped, message...)
	wrapped = append(wrapped, "</Bytes>"...)
	return verify(wrapped)
}

func executorSS58PublicKey(address string) ([32]byte, bool) {
	var publicKey [32]byte
	raw, err := base58.Decode(address)
	if err != nil || len(raw) != 35 || raw[0] >= 64 {
		return publicKey, false
	}
	hasher, err := blake2b.New512(nil)
	if err != nil {
		return publicKey, false
	}
	_, _ = hasher.Write([]byte("SS58PRE"))
	_, _ = hasher.Write(raw[:33])
	checksum := hasher.Sum(nil)
	if checksum[0] != raw[33] || checksum[1] != raw[34] {
		return publicKey, false
	}
	copy(publicKey[:], raw[1:33])
	return publicKey, true
}
