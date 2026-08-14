package upload

import (
	"encoding/hex"
	"testing"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
)

func TestVerifySr25519AcceptsBytesWrappedSignature(t *testing.T) {
	secret, public, err := schnorrkel.GenerateKeypair()
	if err != nil {
		t.Fatal(err)
	}
	hotkey := testSS58(public.Encode())
	message := []byte(hotkey + ":" + "abc123")
	wrapped := append(append([]byte("<Bytes>"), message...), []byte("</Bytes>")...)
	signature, err := secret.Sign(schnorrkel.NewSigningContext([]byte("substrate"), wrapped))
	if err != nil {
		t.Fatal(err)
	}
	encoded := signature.Encode()
	if !verifySr25519(hotkey, message, hex.EncodeToString(encoded[:])) {
		t.Fatal("wrapped sr25519 signature was rejected")
	}
}
