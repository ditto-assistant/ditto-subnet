package inference

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"testing"
	"time"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
	"github.com/google/uuid"
	"github.com/mr-tron/base58"
	"golang.org/x/crypto/blake2b"
)

// ss58Encode is the test-side inverse of ss58Decode (format 42, the
// substrate generic prefix Bittensor uses).
func ss58Encode(pub [32]byte) string {
	data := append([]byte{42}, pub[:]...)
	hasher, _ := blake2b.New512(nil)
	hasher.Write([]byte("SS58PRE"))
	hasher.Write(data)
	sum := hasher.Sum(nil)
	return base58.Encode(append(data, sum[0], sum[1]))
}

func TestIsoformatMicro(t *testing.T) {
	ts := time.Date(2026, 8, 13, 12, 0, 0, 0, time.UTC)
	if got := isoformatMicro(ts); got != "2026-08-13T12:00:00.000000+00:00" {
		t.Fatalf("isoformat: %q", got)
	}
	ts = time.Date(2026, 8, 13, 12, 0, 0, 123456000, time.FixedZone("X", 3600))
	if got := isoformatMicro(ts); got != "2026-08-13T11:00:00.123456+00:00" {
		t.Fatalf("isoformat tz-normalized: %q", got)
	}
}

func TestSr25519RoundTrip(t *testing.T) {
	secret, pub, err := schnorrkel.GenerateKeypair()
	if err != nil {
		t.Fatalf("keypair: %v", err)
	}
	hotkey := ss58Encode(pub.Encode())
	grantID := uuid.New()
	nonce := uuid.New()
	requestedAt := time.Now().UTC()
	message := exchangeMessage(hotkey, grantID, "A_43-char-broker-key-a_b_c_d_e_f_g_h_i_j_k=", nonce, requestedAt)

	sig, err := secret.Sign(schnorrkel.NewSigningContext([]byte("substrate"), message))
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	encoded := sig.Encode()
	sigHex := hex.EncodeToString(encoded[:])

	if !verifySr25519(hotkey, message, sigHex) {
		t.Fatal("valid signature must verify")
	}
	if verifySr25519(hotkey, append(message, 'x'), sigHex) {
		t.Fatal("tampered message must not verify")
	}
	otherSecret, _, _ := schnorrkel.GenerateKeypair()
	otherSig, _ := otherSecret.Sign(schnorrkel.NewSigningContext([]byte("substrate"), message))
	otherEncoded := otherSig.Encode()
	if verifySr25519(hotkey, message, hex.EncodeToString(otherEncoded[:])) {
		t.Fatal("wrong key must not verify")
	}
	if verifySr25519(hotkey, message, "zz") {
		t.Fatal("malformed hex must not verify")
	}
	if verifySr25519("not-an-address", message, sigHex) {
		t.Fatal("malformed ss58 must not verify")
	}
}

func TestSs58DecodeChecksum(t *testing.T) {
	_, pub, _ := schnorrkel.GenerateKeypair()
	address := ss58Encode(pub.Encode())
	decoded, err := ss58Decode(address)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if decoded != pub.Encode() {
		t.Fatal("decoded key mismatch")
	}
	// Corrupt one character.
	corrupted := []byte(address)
	if corrupted[10] == '3' {
		corrupted[10] = '4'
	} else {
		corrupted[10] = '3'
	}
	if _, err := ss58Decode(string(corrupted)); err == nil {
		t.Fatal("corrupted address must fail the checksum")
	}
}

func TestProxyMessageAndEd25519(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatalf("ed25519: %v", err)
	}
	stored := trimBase64Padding(base64.URLEncoding.EncodeToString(pub))
	decoded, err := decodeBrokerPublicKey(stored)
	if err != nil {
		t.Fatalf("decode broker key: %v", err)
	}
	grantID := uuid.New()
	nonce := uuid.New()
	requestedAt := time.Date(2026, 8, 13, 9, 30, 0, 500000000, time.UTC)
	body := []byte(`{"messages":[]}`)
	message := proxyMessage(grantID, 3, nonce, requestedAt, body)
	wantPrefix := "ditto-inference:v1:" + grantID.String() + ":3:" + nonce.String() + ":2026-08-13T09:30:00.500000+00:00:"
	if string(message[:len(wantPrefix)]) != wantPrefix {
		t.Fatalf("proxy message shape: %q", message)
	}
	sig := ed25519.Sign(priv, message)
	proof := trimBase64Padding(base64.URLEncoding.EncodeToString(sig))
	rawProof, err := decodeProof(proof)
	if err != nil {
		t.Fatalf("decode proof: %v", err)
	}
	if !ed25519.Verify(decoded, message, rawProof) {
		t.Fatal("proof must verify")
	}
	if _, err := decodeBrokerPublicKey("short"); err == nil {
		t.Fatal("short key must fail")
	}
}

func TestBearerShape(t *testing.T) {
	bearer, err := newBearer()
	if err != nil {
		t.Fatalf("bearer: %v", err)
	}
	if len(bearer) != 43 {
		t.Fatalf("token_urlsafe(32) is 43 chars, got %d", len(bearer))
	}
	if len(bearerDigest(bearer)) != 64 {
		t.Fatalf("digest must be sha256 hex")
	}
}
