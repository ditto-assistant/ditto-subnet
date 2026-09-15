package codinghostedinput

import (
	"context"
	"crypto/sha256"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// TaskBinding is the reviewed selection a candidate execution profile is
// checked against. It is operator review data, never a runtime authority.
type TaskBinding struct {
	CatalogIndex         int
	TaskCommitmentSHA256 string
	PrivateReleaseSHA256 string
	CorpusReleaseID      string
	MaxPatchBytes        int64
}

// CheckTaskProfile runs the exact launch-time task compilation that Prepare
// applies, using a throwaway authority, so a reviewed execution profile cannot
// first fail at launch. It starts no session and returns no private data.
func CheckTaskProfile(ctx context.Context, binding TaskBinding, profile Profile, objects map[string][]byte) error {
	if ctx == nil || len(objects) != len(roles) {
		return ErrInput
	}
	profileSHA, err := ProfileDigest(profile)
	if err != nil {
		return ErrInput
	}
	dryRun := fmt.Sprintf("%x", sha256.Sum256([]byte("dittobench-coding-hosted-profile-check-v1")))
	expected := Expected{
		EvaluationID: uuid.NewString(), AttemptID: uuid.NewString(), WorkerID: uuid.NewString(),
		AssignmentSHA256: dryRun, RegistrationSHA256: dryRun, ExecutionProfileSHA256: profileSHA,
		TaskCommitmentSHA256: binding.TaskCommitmentSHA256, PrivateReleaseSHA256: binding.PrivateReleaseSHA256,
		CorpusReleaseID: binding.CorpusReleaseID, CatalogIndex: binding.CatalogIndex,
		MaxPatchBytes: binding.MaxPatchBytes, DeadlineUnix: time.Now().Add(55 * time.Minute).Unix(),
	}
	if !expectedValid(expected) || profile.ResourcePolicy.CandidateLimits.MaxPatchBytes != binding.MaxPatchBytes {
		return ErrInput
	}
	for _, role := range roles {
		limit := int64(4 << 20)
		if role == "visible_bundle" {
			limit = min(128<<20, profile.ResourcePolicy.CandidateLimits.MaxBundleBytes)
		}
		if role == "catalog_record" {
			limit = 64 << 10
		}
		if size := int64(len(objects[role])); size <= 0 || size > limit {
			return ErrInput
		}
	}
	call, cancel := context.WithTimeout(ctx, 180*time.Second)
	defer cancel()
	prepared, err := compile(call, expected, profile, objects)
	if err != nil {
		return ErrInput
	}
	prepared.Close()
	return nil
}
