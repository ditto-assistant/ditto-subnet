package codingcontract

import (
	"testing"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
	"github.com/mr-tron/base58"
	"golang.org/x/crypto/blake2b"
)

func TestVerifyExecutorSR25519AcceptsRawAndWrappedBittensorSignatures(t *testing.T) {
	secret, public, err := schnorrkel.GenerateKeypair()
	if err != nil {
		t.Fatal(err)
	}
	hotkey := executorTestSS58(t, public.Encode())
	message := []byte("dittobench executor control test")
	for name, payload := range map[string][]byte{
		"raw":     message,
		"wrapped": append(append([]byte("<Bytes>"), message...), []byte("</Bytes>")...),
	} {
		t.Run(name, func(t *testing.T) {
			signature, signErr := secret.Sign(
				schnorrkel.NewSigningContext([]byte("substrate"), payload),
			)
			if signErr != nil {
				t.Fatal(signErr)
			}
			encoded := signature.Encode()
			if !VerifyExecutorSR25519(hotkey, message, encoded[:]) {
				t.Fatal("valid executor signature was rejected")
			}
		})
	}
	if VerifyExecutorSR25519(hotkey, message, make([]byte, 64)) ||
		VerifyExecutorSR25519("5invalid", message, make([]byte, 64)) ||
		VerifyExecutorSR25519(hotkey, message, make([]byte, 63)) {
		t.Fatal("malformed executor signature was accepted")
	}
}

func executorTestSS58(t *testing.T, public [32]byte) string {
	t.Helper()
	raw := append([]byte{42}, public[:]...)
	hasher, err := blake2b.New512(nil)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = hasher.Write([]byte("SS58PRE"))
	_, _ = hasher.Write(raw)
	checksum := hasher.Sum(nil)
	raw = append(raw, checksum[:2]...)
	return base58.Encode(raw)
}
