package catalog

import (
	"errors"
	"maps"
	"slices"
)

// ApprovalSchema is the curator-signed native controls approval. Peyton signs
// its canonical bytes with the offline curator key (a detached Ed25519
// signature); nothing in this package or any collector can create one.
const ApprovalSchema = "dittobench-coding-native-controls-approval-v3"

// ApprovalKeys is the approval's closed key set, identical to native.policy.
var ApprovalKeys = []string{
	"binding_sha256", "boot_id", "controls", "curator_signing_key_sha256", "daemon_identity",
	"evidence_sha256", "evidence_tool_sha256", "expires_at_unix", "helper_sha256", "images",
	"issued_at_unix", "machine_id_sha256", "max_jobs", "plan_sha256", "profile_pins",
	"purpose", "release_manifest_sha256", "runner_sha256", "schema", "shadow_only",
	"source_revision", "weight_eligible",
}

// ErrApproval marks bytes that are not a canonical approval document.
var ErrApproval = errors.New("approval is not a canonical native controls approval")

// ApprovalDaemonIdentitySHA256 returns the digest of the daemon identity a
// canonical approval names, the value evidence records carry as
// host.daemon_identity_sha256. It checks shape only and verifies no signature:
// a collector may use it to refuse a different daemon, never to authorize.
func ApprovalDaemonIdentitySHA256(raw []byte) (string, error) {
	decoded, err := ParseCanonical(raw)
	if err != nil {
		return "", ErrApproval
	}
	object, ok := decoded.(map[string]any)
	if !ok || !slices.Equal(slices.Sorted(maps.Keys(object)), ApprovalKeys) || object["schema"] != ApprovalSchema {
		return "", ErrApproval
	}
	identity, ok := object["daemon_identity"].(map[string]any)
	if !ok {
		return "", ErrApproval
	}
	encoded, err := Canonical(identity)
	if err != nil {
		return "", ErrApproval
	}
	return digestOf(encoded), nil
}
