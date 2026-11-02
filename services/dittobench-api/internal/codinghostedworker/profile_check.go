package codinghostedworker

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/google/uuid"
)

// GradingBinding is the reviewed selection a candidate grading profile is
// checked against. It is operator review data, never a runtime authority.
type GradingBinding struct {
	CatalogIndex         int
	TaskCommitmentSHA256 string
	PrivateReleaseSHA256 string
}

var gradingObjectLimits = map[string]int64{
	"catalog_record": 64 << 10, "visible_bundle": 128 << 20, "grader_bundle": 64 << 20,
	"runtime_policy": 4 << 20, "resource_profile": 4 << 20,
}

// GradingProfileBytes returns the exact canonical bytes the worker and Platform
// accept for a valid hosted grading profile.
func GradingProfileBytes(profile GradingProfile) ([]byte, error) {
	if profile.Validate() != nil || profile.GraderContractSHA256 != codinggrader.HostedGraderContractSHA256() {
		return nil, ErrAttempt
	}
	body, err := canonicalGrading(profile)
	if err != nil || len(body) > 65536 || codingcontract.RequireExactCanonicalJSON(body) != nil {
		return nil, ErrAttempt
	}
	return body, nil
}

// CheckGradingProfile applies the launch-time grading input checks to exact
// profile bytes and verified private task objects with a throwaway binding. It
// also requires every driver suite to exist in the bundle that group reads.
func CheckGradingProfile(ctx context.Context, binding GradingBinding, body []byte, objects map[string][]byte) error {
	if ctx == nil || len(objects) != len(gradingObjectLimits) || len(body) == 0 || len(body) > 65536 {
		return ErrAttempt
	}
	for role, limit := range gradingObjectLimits {
		if size := int64(len(objects[role])); size <= 0 || size > limit {
			return ErrAttempt
		}
	}
	var profile GradingProfile
	if codingcontract.RequireExactCanonicalJSON(body) != nil || json.Unmarshal(body, &profile) != nil {
		return ErrAttempt
	}
	if exact, err := GradingProfileBytes(profile); err != nil || !bytes.Equal(exact, body) {
		return ErrAttempt
	}
	var task codingcontract.PrivateCatalogTaskV2
	if codingcontract.RequireExactCanonicalJSON(objects["catalog_record"]) != nil || json.Unmarshal(objects["catalog_record"], &task) != nil || task.Validate() != nil || task.TaskCommitmentSHA256 != binding.TaskCommitmentSHA256 || int(task.CatalogIndex) != binding.CatalogIndex || task.PrivateReleaseSHA256 != binding.PrivateReleaseSHA256 {
		return ErrAttempt
	}
	grader := objects["grader_bundle"]
	if sum(objects["runtime_policy"]) != task.RuntimePolicySHA256 || sum(objects["resource_profile"]) != task.ResourceProfileSHA256 || sum(grader) != profile.GraderBundleSHA256 || int64(len(grader)) > profile.ResourcePolicy.ProtectedLimits.MaxBundleBytes {
		return ErrAttempt
	}
	call, cancel := context.WithTimeout(ctx, 180*time.Second)
	defer cancel()
	tree, err := privateGraderTree(call, grader, profile.ResourcePolicy.ProtectedLimits)
	if err != nil || tree != task.HiddenGraderTreeSHA256 {
		return ErrAttempt
	}
	snapshot, err := codingrunner.CompileHostedSnapshot(call, objects["visible_bundle"], sum(objects["visible_bundle"]), profile.ResourcePolicy.CandidateLimits)
	if err != nil || snapshot.CapsuleTreeSHA256 != task.VisibleSnapshotTreeSHA256 {
		return ErrAttempt
	}
	attempt := uuid.NewString()
	dryRun := sum([]byte("dittobench-coding-hosted-profile-check-v1"))
	source := codingsource.HostedBinding{HarnessInstanceID: "profile-check", AgentArtifactSHA256: dryRun, EvaluationID: uuid.NewString(), AttemptID: attempt, WorkerID: uuid.NewString(), AssignmentSHA256: dryRun, ProfileCapabilityID: "hosted-" + attempt, Deadline: time.Now().Add(55 * time.Minute)}
	if _, err := profile.manifest(source, codingrunner.FrozenSubmission{VisibleBundleSHA256: snapshot.Identity.VisibleBundleSHA256, BaseTreeSHA256: snapshot.Identity.TreeSHA256}); err != nil {
		return ErrAttempt
	}
	suites := map[string][]byte{"hidden": grader, "visible": snapshot.Bundle}
	for _, group := range profile.TestGroups {
		if err := requireSuite(group, suites[group.Group]); err != nil {
			return ErrAttempt
		}
	}
	return nil
}

func sum(body []byte) string { return fmt.Sprintf("%x", sha256.Sum256(body)) }

func requireSuite(group codinggrader.TestGroupSpec, bundle []byte) error {
	argv := group.Command.Argv
	groups, suites := 0, []string{}
	for i := 1; i < len(argv); i++ {
		if argv[i] != "--group" && argv[i] != "--suite" {
			continue
		}
		if i+1 >= len(argv) {
			return errors.New("flag value missing")
		}
		if argv[i] == "--group" {
			if argv[i+1] != group.Group {
				return errors.New("group mismatch")
			}
			groups++
		} else {
			suites = append(suites, argv[i+1])
		}
		i++
	}
	if groups != 1 || len(suites) > 1 || bundle == nil {
		return errors.New("driver command shape invalid")
	}
	if len(suites) == 0 {
		return nil
	}
	want := path.Clean(suites[0])
	if want != suites[0] || path.IsAbs(want) || want == "." || want == ".." || len(want) > 2 && want[:3] == "../" {
		return errors.New("suite path invalid")
	}
	reader := tar.NewReader(bytes.NewReader(bundle))
	for {
		header, err := reader.Next()
		if err == io.EOF {
			return errors.New("suite absent")
		}
		if err != nil {
			return err
		}
		if header.Typeflag == tar.TypeReg && path.Clean(header.Name) == want {
			return nil
		}
	}
}
