package codingcanary

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"net/url"
	"regexp"
	"strings"
	"time"
)

var bearerPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{32,128}$`)

func validGrant(grant Grant, request Request, now time.Time) bool {
	return grant.Schema == "dittobench-coding-certification-inference-exchange-v1" &&
		grant.CodingContractVersion == 1 && !grant.WeightEligible && grant.Status == "active" &&
		validUUID(grant.GrantID) && grant.LeaseID == request.LeaseID &&
		grant.CaseID == publicCanaryTaskID && grant.ProfileCapabilityID == publicCanaryProfileID &&
		validSHA256(grant.InferenceGrantSHA256) && grant.Generation > 0 && grant.Generation <= 1<<31-1 &&
		grant.RequestBudget > 0 && grant.RequestBudget <= 256 &&
		grant.PromptTokenBudget > 0 && grant.CompletionTokenBudget > 0 && grant.CostBudgetUSDMicros > 0 &&
		bearerPattern.MatchString(grant.Bearer) && validProxyURL(grant.ProxyURL) &&
		bearerPattern.MatchString(grant.RevokeBearer) && grant.RevokeBearer != grant.Bearer &&
		validRevokeURL(grant.RevokeURL) && validBrokerPair(grant.BrokerPublicKey, grant.BrokerPrivateKey) &&
		grant.ExpiresAt.After(now) && !grant.ExpiresAt.After(request.Deadline)
}

func validBrokerPair(public, private string) bool {
	seed, err := base64.RawURLEncoding.DecodeString(strings.TrimSuffix(private, "="))
	if err != nil || len(seed) != ed25519.PrivateKeySize {
		return false
	}
	decoded, err := base64.RawURLEncoding.DecodeString(strings.TrimSuffix(public, "="))
	if err != nil || len(decoded) != ed25519.PublicKeySize {
		return false
	}
	derived, ok := ed25519.PrivateKey(seed).Public().(ed25519.PublicKey)
	return ok && bytes.Equal(decoded, derived)
}

func validProxyURL(value string) bool {
	if value == "" || len(value) > 2_048 {
		return false
	}
	parsed, err := url.ParseRequestURI(value)
	return err == nil && parsed.Scheme == "https" && parsed.Hostname() != "" && parsed.User == nil &&
		parsed.RawQuery == "" && parsed.Fragment == "" &&
		parsed.Path == "/api/v1/inference/coding/chat/completions" &&
		(parsed.Port() == "" || parsed.Port() == "443")
}

func validRevokeURL(value string) bool {
	if value == "" || len(value) > 2_048 {
		return false
	}
	parsed, err := url.ParseRequestURI(value)
	return err == nil && parsed.Scheme == "https" && parsed.Hostname() != "" && parsed.User == nil &&
		parsed.RawQuery == "" && parsed.Fragment == "" &&
		strings.HasSuffix(parsed.Path, "/api/v1/validator/coding-shadow/inference-revoke-capability") &&
		(parsed.Port() == "" || parsed.Port() == "443")
}

func decodeBrokerPrivateKey(value string) (ed25519.PrivateKey, bool) {
	seed, err := base64.RawURLEncoding.DecodeString(strings.TrimSuffix(value, "="))
	if err != nil || len(seed) != ed25519.PrivateKeySize {
		return nil, false
	}
	return ed25519.PrivateKey(seed), true
}
