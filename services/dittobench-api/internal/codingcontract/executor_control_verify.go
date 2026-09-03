package codingcontract

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"sync"
	"time"
)

var ErrExecutorControl = errors.New("coding executor control verification failed")

type ExecutorSignatureVerifier func(hotkey string, message, signature []byte) bool

type ExecutorControlVerifier struct {
	mu      sync.Mutex
	now     func() time.Time
	verify  ExecutorSignatureVerifier
	nonces  map[string]time.Time
	maximum int
}

func NewExecutorControlVerifier(now func() time.Time, verify ExecutorSignatureVerifier, maximum int) (*ExecutorControlVerifier, error) {
	if now == nil || verify == nil || maximum < 1 || maximum > 65536 {
		return nil, ErrExecutorControl
	}
	return &ExecutorControlVerifier{now: now, verify: verify, nonces: make(map[string]time.Time), maximum: maximum}, nil
}

func (verifier *ExecutorControlVerifier) Verify(value ExecutorControlEnvelope, body []byte) error {
	if verifier == nil || value.Validate() != nil || sha256Hex(body) != value.RequestBodySHA256 {
		return ErrExecutorControl
	}
	now := verifier.now().UTC()
	if now.Before(value.IssuedAt.Add(-30*time.Second)) || !now.Before(value.ExpiresAt) {
		return ErrExecutorControl
	}
	message, err := ExecutorControlSigningMessage(value)
	signature, decodeErr := hex.DecodeString(value.Signature)
	if err != nil || decodeErr != nil || !verifier.verify(value.ValidatorHotkey, message, signature) {
		return ErrExecutorControl
	}
	verifier.mu.Lock()
	defer verifier.mu.Unlock()
	for nonce, expiry := range verifier.nonces {
		if !now.Before(expiry) {
			delete(verifier.nonces, nonce)
		}
	}
	if _, exists := verifier.nonces[value.Nonce]; exists || len(verifier.nonces) >= verifier.maximum {
		return ErrExecutorControl
	}
	verifier.nonces[value.Nonce] = value.ExpiresAt
	return nil
}

func sha256Hex(body []byte) string {
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:])
}
