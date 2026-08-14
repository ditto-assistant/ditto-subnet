package upload

import (
	"encoding/hex"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
	"github.com/mr-tron/base58"
	"golang.org/x/crypto/blake2b"
)

func ss58Decode(address string) ([32]byte, bool) {
	var pub [32]byte
	raw, err := base58.Decode(address)
	if err != nil || len(raw) != 35 || raw[0] >= 64 {
		return pub, false
	}
	hasher, err := blake2b.New512(nil)
	if err != nil {
		return pub, false
	}
	_, _ = hasher.Write([]byte("SS58PRE"))
	_, _ = hasher.Write(raw[:33])
	sum := hasher.Sum(nil)
	if sum[0] != raw[33] || sum[1] != raw[34] {
		return pub, false
	}
	copy(pub[:], raw[1:33])
	return pub, true
}

// verifySr25519 mirrors substrateinterface.Keypair.verify, including the
// polkadot-js <Bytes> fallback accepted by the Python upload endpoint.
func verifySr25519(hotkey string, message []byte, signatureHex string) bool {
	pubBytes, ok := ss58Decode(hotkey)
	if !ok {
		return false
	}
	sigBytes, err := hex.DecodeString(signatureHex)
	if err != nil || len(sigBytes) != 64 {
		return false
	}
	pub, err := schnorrkel.NewPublicKey(pubBytes)
	if err != nil {
		return false
	}
	var sigArr [64]byte
	copy(sigArr[:], sigBytes)
	sig := new(schnorrkel.Signature)
	if err := sig.Decode(sigArr); err != nil {
		return false
	}
	verify := func(msg []byte) bool {
		valid, err := pub.Verify(sig, schnorrkel.NewSigningContext([]byte("substrate"), msg))
		return err == nil && valid
	}
	if verify(message) {
		return true
	}
	wrapped := append(append([]byte("<Bytes>"), message...), []byte("</Bytes>")...)
	return verify(wrapped)
}
