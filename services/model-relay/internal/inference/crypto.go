// Package inference implements the relay's inference plane: the three
// POST /api/v1/inference/* endpoints, the DB admission/settlement
// orchestration around the sqlc query layer, and the upstream provider
// calls. Wire behavior mirrors apps/platform/ditto/api_server/endpoints/
// inference.py byte-for-byte wherever the two runtimes allow.
package inference

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"strconv"
	"time"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
	"github.com/google/uuid"
	"github.com/mr-tron/base58"
	"golang.org/x/crypto/blake2b"
)

// isoformatMicro renders a timestamp exactly like Python's
// datetime.astimezone(UTC).isoformat(timespec="microseconds"):
// always six fractional digits and a "+00:00" offset.
func isoformatMicro(t time.Time) string {
	return t.UTC().Format("2006-01-02T15:04:05.000000") + "+00:00"
}

// exchangeMessage is the sr25519-signed exchange payload:
//
//	validator-inference:v1:{hotkey}:{grant_id}:{key sans '='}:{nonce}:{requested_at}
func exchangeMessage(validatorHotkey string, grantID uuid.UUID, brokerPublicKey string, nonce uuid.UUID, requestedAt time.Time) []byte {
	return []byte(fmt.Sprintf("validator-inference:v1:%s:%s:%s:%s:%s",
		validatorHotkey, grantID.String(), trimBase64Padding(brokerPublicKey),
		nonce.String(), isoformatMicro(requestedAt)))
}

// proxyMessage is the Ed25519-signed per-call proof payload:
//
//	ditto-inference:v1:{grant_id}:{generation}:{nonce}:{requested_at}:{sha256hex(body)}
func proxyMessage(grantID uuid.UUID, generation int64, nonce uuid.UUID, requestedAt time.Time, body []byte) []byte {
	digest := sha256.Sum256(body)
	return []byte(fmt.Sprintf("ditto-inference:v1:%s:%s:%s:%s:%s",
		grantID.String(), strconv.FormatInt(generation, 10), nonce.String(),
		isoformatMicro(requestedAt), hex.EncodeToString(digest[:])))
}

func trimBase64Padding(s string) string {
	for len(s) > 0 && s[len(s)-1] == '=' {
		s = s[:len(s)-1]
	}
	return s
}

// bearerDigest is sha256 hex of the opaque bearer, matching
// ditto/db/queries/inference.py::bearer_digest.
func bearerDigest(bearer string) string {
	d := sha256.Sum256([]byte(bearer))
	return hex.EncodeToString(d[:])
}

// constantTimeEqual mirrors secrets.compare_digest on two hex digests.
func constantTimeEqual(a, b string) bool {
	if len(a) != len(b) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}

// newBearer mirrors secrets.token_urlsafe(32): 32 random bytes,
// urlsafe-base64 without padding (43 chars).
func newBearer() (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b[:]), nil
}

// ss58Decode extracts the 32-byte public key from an SS58 address,
// verifying the blake2b checksum. Any malformed input returns an error
// (the caller treats it as "signature did not verify", matching the Python
// Keypair constructor's ValueError handling).
func ss58Decode(address string) ([32]byte, error) {
	var pub [32]byte
	raw, err := base58.Decode(address)
	if err != nil {
		return pub, fmt.Errorf("ss58: %w", err)
	}
	// One-byte format prefix (simple accounts, format < 64) + 32-byte key +
	// 2-byte checksum.
	if len(raw) != 35 {
		return pub, fmt.Errorf("ss58: unexpected payload length %d", len(raw))
	}
	if raw[0] >= 64 {
		return pub, fmt.Errorf("ss58: unsupported format prefix %d", raw[0])
	}
	hasher, err := blake2b.New512(nil)
	if err != nil {
		return pub, err
	}
	hasher.Write([]byte("SS58PRE"))
	hasher.Write(raw[:33])
	sum := hasher.Sum(nil)
	if sum[0] != raw[33] || sum[1] != raw[34] {
		return pub, fmt.Errorf("ss58: checksum mismatch")
	}
	copy(pub[:], raw[1:33])
	return pub, nil
}

// verifySr25519 verifies a Bittensor validator signature: sr25519 over the
// "substrate" signing context, with the polkadot-js "<Bytes>...</Bytes>"
// wrapped form accepted as a fallback exactly like
// substrateinterface.Keypair.verify. Malformed inputs return false, never an
// error (the Python helper catches ValueError/TypeError).
func verifySr25519(hotkey string, message []byte, signatureHex string) bool {
	pubBytes, err := ss58Decode(hotkey)
	if err != nil {
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
		ok, err := pub.Verify(sig, schnorrkel.NewSigningContext([]byte("substrate"), msg))
		return err == nil && ok
	}
	if verify(message) {
		return true
	}
	wrapped := append(append([]byte("<Bytes>"), message...), []byte("</Bytes>")...)
	return verify(wrapped)
}

// decodeBrokerPublicKey restores urlsafe-base64 padding and requires an
// exact 32-byte Ed25519 public key, mirroring _decode_public_key (which maps
// any failure to 401 "invalid inference proof").
func decodeBrokerPublicKey(value string) (ed25519.PublicKey, error) {
	if pad := len(value) % 4; pad != 0 {
		value += "===="[:4-pad]
	}
	raw, err := base64.URLEncoding.DecodeString(value)
	if err != nil {
		return nil, err
	}
	if len(raw) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("ed25519 public key must be %d bytes, got %d", ed25519.PublicKeySize, len(raw))
	}
	return ed25519.PublicKey(raw), nil
}

// decodeProof restores urlsafe-base64 padding on the X-Ditto-Proof header.
func decodeProof(value string) ([]byte, error) {
	if pad := len(value) % 4; pad != 0 {
		value += "===="[:4-pad]
	}
	return base64.URLEncoding.DecodeString(value)
}
