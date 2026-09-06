package codinghostedworker

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/google/uuid"
)

type GradingProfile struct {
	Schema               string                       `json:"schema"`
	ImageDigest          string                       `json:"image_digest"`
	GraderContractSHA256 string                       `json:"grader_contract_sha256"`
	GraderBundleSHA256   string                       `json:"grader_bundle_sha256"`
	TestManifestSHA256   string                       `json:"test_manifest_sha256"`
	ResourcePolicy       codinggrader.ResourcePolicy  `json:"resource_policy"`
	Build                codinggrader.BuildSpec       `json:"build"`
	TestGroups           []codinggrader.TestGroupSpec `json:"test_groups"`
	ExecutionTimeout     time.Duration                `json:"execution_timeout"`
}

func canonicalGrading(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var object any
	if err := decoder.Decode(&object); err != nil {
		return nil, err
	}
	var body bytes.Buffer
	encoder := json.NewEncoder(&body)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(object); err != nil {
		return nil, err
	}
	return body.Bytes(), nil
}

func (p GradingProfile) manifest(source codingsource.HostedBinding, sub codingrunner.FrozenSubmission) (codinggrader.HostedManifest, error) {
	if p.Schema != "dittobench-coding-hosted-grading-profile-v2" || p.GraderContractSHA256 != codinggrader.HostedGraderContractSHA256() {
		return codinggrader.HostedManifest{}, ErrAttempt
	}
	m := codinggrader.HostedManifest{CodingContractVersion: 2, CaseID: source.AttemptID, VariantID: "hosted-private", VisibleBundleSHA256: sub.VisibleBundleSHA256, BaseTreeSHA256: sub.BaseTreeSHA256, GraderContractSHA256: p.GraderContractSHA256, GraderBundleSHA256: p.GraderBundleSHA256, GraderImageDigest: p.ImageDigest, GraderPlatform: "linux/amd64", TestManifestSHA256: p.TestManifestSHA256, Deadline: source.Deadline, ExecutionTimeout: p.ExecutionTimeout, ResourcePolicy: p.ResourcePolicy, Build: p.Build, TestGroups: p.TestGroups}
	var err error
	m.ResourceProfileSHA256, err = codinggrader.HostedResourceProfileSHA256(p.ResourcePolicy)
	if err != nil {
		return m, ErrAttempt
	}
	m.GraderPlanSHA256, err = codinggrader.HostedGraderPlanSHA256(m)
	if err != nil || m.Validate(time.Now()) != nil {
		return m, ErrAttempt
	}
	return m, nil
}

type gradingHeader struct {
	Schema        string `json:"schema"`
	ClaimID       string `json:"claim_id"`
	ProfileSHA    string `json:"grading_profile_sha256"`
	PatchSHA      string `json:"frozen_patch_sha256"`
	PlanSHA       string `json:"grader_plan_sha256"`
	GraderTreeSHA string `json:"-"`
	Objects       []struct {
		Role string `json:"role"`
		SHA  string `json:"sha256"`
		Size int64  `json:"size_bytes"`
	} `json:"objects"`
}

func privateGraderTree(ctx context.Context, body []byte, limits codingrunner.Limits) (string, error) {
	if _, err := codingrunner.InspectBundle(ctx, bytes.NewReader(body), limits); err != nil {
		return "", ErrAttempt
	}
	reader := tar.NewReader(bytes.NewReader(body))
	files := []map[string]any{}
	for {
		h, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil || ctx.Err() != nil {
			return "", ErrAttempt
		}
		if h.Typeflag == tar.TypeDir {
			continue
		}
		if h.Typeflag != tar.TypeReg && h.Typeflag != tar.TypeRegA {
			return "", ErrAttempt
		}
		hash := sha256.New()
		n, err := io.Copy(hash, reader)
		if err != nil || n != h.Size {
			return "", ErrAttempt
		}
		files = append(files, map[string]any{"path": h.Name, "sha256": fmt.Sprintf("%x", hash.Sum(nil)), "size_bytes": n})
	}
	sort.Slice(files, func(i, j int) bool { return files[i]["path"].(string) < files[j]["path"].(string) })
	encoded, err := canonicalGrading(files)
	if err != nil {
		return "", ErrAttempt
	}
	return fmt.Sprintf("%x", sha256.Sum256(encoded)), nil
}

func (c *ControlClient) gradingInputs(ctx context.Context, source codingsource.HostedBinding, freeze codingrunner.FreezeResult, evidence string) (gradingHeader, map[string][]byte, codinggrader.HostedManifest, error) {
	fail := func() (gradingHeader, map[string][]byte, codinggrader.HostedManifest, error) {
		return gradingHeader{}, nil, codinggrader.HostedManifest{}, ErrAttempt
	}
	if len(c.gradingProfile) == 0 || len(c.gradingProfile) > 65536 || !digest(c.config.GradingProfileSHA256) || fmt.Sprintf("%x", sha256.Sum256(c.gradingProfile)) != c.config.GradingProfileSHA256 || freeze.Submission == nil {
		return fail()
	}
	var profile GradingProfile
	if codingcontract.ValidateJSONDocument(c.gradingProfile, 65536) != nil || json.Unmarshal(c.gradingProfile, &profile) != nil {
		return fail()
	}
	manifest, err := profile.manifest(source, *freeze.Submission)
	if err != nil {
		return fail()
	}
	body, err := encodePrivateFreeze(freeze)
	if err != nil {
		return fail()
	}
	header := map[string]any{"completed_run": true, "freeze_sha256": fmt.Sprintf("%x", sha256.Sum256(body)), "freeze_size": len(body), "transcript_sha256": freeze.Submission.AuthoringTranscriptSHA256, "transcript_size": freeze.Submission.AuthoringTranscriptBytes}
	conn, reply, err := c.exchange(ctx, source, "grading", map[string]any{"retention": header, "evidence_sha256": evidence}, func(w io.Writer) error { _, err := io.Copy(w, bytes.NewReader(body)); return err })
	if err != nil {
		return fail()
	}
	defer conn.Close()
	if reply.PayloadKind != "grading" {
		return fail()
	}
	var prefix [4]byte
	if _, err := io.ReadFull(conn, prefix[:]); err != nil {
		return fail()
	}
	n := binary.BigEndian.Uint32(prefix[:])
	if n == 0 || n > 16384 {
		return fail()
	}
	raw := make([]byte, n)
	if _, err := io.ReadFull(conn, raw); err != nil || codingcontract.ValidateJSONDocument(raw, 16384) != nil {
		return fail()
	}
	var value gradingHeader
	if json.Unmarshal(raw, &value) != nil {
		return fail()
	}
	id, err := uuid.Parse(value.ClaimID)
	if err != nil || id == uuid.Nil || id.String() != value.ClaimID || value.Schema != "dittobench-coding-hosted-grading-inputs-v2" || value.ProfileSHA != c.config.GradingProfileSHA256 || value.PatchSHA != freeze.Submission.FrozenPatchSHA256 || value.PlanSHA != manifest.GraderPlanSHA256 || len(value.Objects) != 5 {
		return fail()
	}
	roles := []string{"catalog_record", "visible_bundle", "grader_bundle", "runtime_policy", "resource_profile"}
	objects := map[string][]byte{}
	for i, item := range value.Objects {
		maximum := int64(4 << 20)
		if i == 0 {
			maximum = 64 << 10
		}
		if i == 1 {
			maximum = 128 << 20
		}
		if i == 2 {
			maximum = 64 << 20
		}
		if item.Role != roles[i] || item.Size <= 0 || item.Size > maximum || !digest(item.SHA) {
			return fail()
		}
		if i == 2 {
			continue
		}
		bytes := make([]byte, item.Size)
		if _, err := io.ReadFull(conn, bytes); err != nil || fmt.Sprintf("%x", sha256.Sum256(bytes)) != item.SHA {
			return fail()
		}
		objects[item.Role] = bytes
	}
	marker := make([]byte, len("DITTO-GRADING-READY-V2\n"))
	if _, err := io.ReadFull(conn, marker); err != nil || string(marker) != "DITTO-GRADING-READY-V2\n" {
		return fail()
	}
	var tail [1]byte
	if n, err := conn.Read(tail[:]); n != 0 || err != io.EOF || ctx.Err() != nil {
		return fail()
	}
	var task codingcontract.PrivateCatalogTaskV2
	if codingcontract.RequireExactCanonicalJSON(objects["catalog_record"]) != nil || json.Unmarshal(objects["catalog_record"], &task) != nil || task.Validate() != nil || task.TaskCommitmentSHA256 != c.config.Expected.TaskCommitmentSHA256 || int(task.CatalogIndex) != c.config.Expected.CatalogIndex || task.PrivateReleaseSHA256 != c.config.Expected.PrivateReleaseSHA256 {
		return fail()
	}
	if fmt.Sprintf("%x", sha256.Sum256(objects["runtime_policy"])) != task.RuntimePolicySHA256 || fmt.Sprintf("%x", sha256.Sum256(objects["resource_profile"])) != task.ResourceProfileSHA256 || value.Objects[2].SHA != manifest.GraderBundleSHA256 {
		return fail()
	}
	value.GraderTreeSHA = task.HiddenGraderTreeSHA256
	snapshot, err := codingrunner.CompileHostedSnapshot(ctx, objects["visible_bundle"], fmt.Sprintf("%x", sha256.Sum256(objects["visible_bundle"])), manifest.ResourcePolicy.CandidateLimits)
	if err != nil || snapshot.CapsuleTreeSHA256 != task.VisibleSnapshotTreeSHA256 || snapshot.Identity.VisibleBundleSHA256 != manifest.VisibleBundleSHA256 || snapshot.Identity.TreeSHA256 != manifest.BaseTreeSHA256 {
		return fail()
	}
	objects["visible_bundle"] = snapshot.Bundle
	return value, objects, manifest, nil
}

type GradingControl interface {
	Grade(context.Context, codingsource.HostedBinding, codingrunner.FreezeResult, string, *codingexecutor.PhaseFactory) ([]byte, error)
	PublishTerminal(context.Context, codingsource.HostedBinding, []byte) (string, error)
}

func (c *ControlClient) Grade(ctx context.Context, source codingsource.HostedBinding, freeze codingrunner.FreezeResult, evidence string, factory *codingexecutor.PhaseFactory) ([]byte, error) {
	header, objects, manifest, err := c.gradingInputs(ctx, source, freeze, evidence)
	if err != nil {
		return nil, ErrAttempt
	}
	defer func() {
		for _, body := range objects {
			clear(body)
		}
	}()
	call, cancel := context.WithDeadline(ctx, source.Deadline)
	defer cancel()
	done := make(chan struct{})
	watch, stop := context.WithCancel(call)
	go func() {
		defer close(done)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-watch.Done():
				return
			case <-ticker.C:
				probe, close := context.WithTimeout(watch, 5*time.Second)
				err := c.simple(probe, source, "check_grading")
				close()
				if err != nil {
					cancel()
					return
				}
			}
		}
	}()
	defer func() { stop(); <-done }()
	var executor codinggrader.Executor
	if c.testGrader != nil {
		executor, err = c.testGrader(call, manifest)
	} else if factory != nil {
		executor, err = factory.HostedGrading(call, manifest)
	} else {
		err = ErrAttempt
	}
	if err != nil {
		executor = nil
	}
	authority := codinggrader.HostedGradingAuthority{HostedReplayAuthority: codingrunner.HostedReplayAuthority{HostedAuthority: codingrunner.HostedAuthority{EvaluationID: source.EvaluationID, AttemptID: source.AttemptID, AssignmentSHA256: source.AssignmentSHA256}, FrozenPatchSHA256: header.PatchSHA}, GraderPlanSHA256: header.PlanSHA, Deadline: source.Deadline}
	result := codinggrader.GradeHosted(call, authority, manifest, *freeze.Submission, bytes.NewReader(objects["visible_bundle"]), func(ctx context.Context) (io.ReadCloser, error) {
		return c.protectedGrader(ctx, source, header, manifest.ResourcePolicy.ProtectedLimits)
	}, executor)
	body, err := canonicalGrading(map[string]any{"schema": "dittobench-coding-hosted-terminal-v2", "claim_id": header.ClaimID, "authoring_evidence_sha256": evidence, "grading_profile_sha256": header.ProfileSHA, "grader_result": result})
	if err != nil || len(body) > 4<<20 {
		return nil, ErrAttempt
	}
	return body, nil
}
func (c *ControlClient) protectedGrader(ctx context.Context, source codingsource.HostedBinding, header gradingHeader, limits codingrunner.Limits) (io.ReadCloser, error) {
	expected := header.Objects[2]
	if expected.Size > limits.MaxBundleBytes {
		return nil, ErrAttempt
	}
	conn, reply, err := c.exchange(ctx, source, "grading_bundle", map[string]any{"claim_id": header.ClaimID}, nil)
	if err != nil {
		return nil, ErrAttempt
	}
	defer conn.Close()
	if reply.PayloadKind != "grading_bundle" || reply.PayloadSize != expected.Size {
		return nil, ErrAttempt
	}
	body := make([]byte, expected.Size)
	if _, err := io.ReadFull(conn, body); err != nil || fmt.Sprintf("%x", sha256.Sum256(body)) != expected.SHA {
		return nil, ErrAttempt
	}
	var end [1]byte
	if n, err := conn.Read(end[:]); n != 0 || err != io.EOF {
		return nil, ErrAttempt
	}
	tree, err := privateGraderTree(ctx, body, limits)
	if err != nil || tree != header.GraderTreeSHA {
		return nil, ErrAttempt
	}
	return io.NopCloser(bytes.NewReader(body)), nil
}

func (c *ControlClient) PublishTerminal(ctx context.Context, source codingsource.HostedBinding, body []byte) (string, error) {
	if len(body) == 0 || len(body) > 4<<20 {
		return "", ErrAttempt
	}
	conn, reply, err := c.exchange(ctx, source, "terminal", map[string]any{"terminal_size": len(body), "terminal_sha256": fmt.Sprintf("%x", sha256.Sum256(body))}, func(w io.Writer) error { _, err := io.Copy(w, bytes.NewReader(body)); return err })
	if conn != nil {
		_ = conn.Close()
	}
	if err != nil || !digest(reply.EvidenceSHA) {
		return "", ErrAttempt
	}
	return reply.EvidenceSHA, nil
}

// RunComplete adds grading and terminal publication to the existing authoring
// lifecycle. Grading itself is one-shot; only captured result bytes may retry.
func (a *Attempt) RunComplete(ctx context.Context) (string, error) {
	if _, err := a.Run(ctx); err != nil {
		return "", err
	}
	return a.Grade(ctx)
}
func (a *Attempt) Grade(ctx context.Context) (string, error) {
	if a == nil || a.worker == nil || ctx == nil {
		return "", ErrAttempt
	}
	a.op.Lock()
	defer a.op.Unlock()
	operation, cancel := context.WithCancel(ctx)
	defer cancel()
	a.mu.Lock()
	a.cancel = cancel
	a.mu.Unlock()
	control, ok := a.worker.deps.control.(GradingControl)
	if !ok || !a.committed || a.aborted || a.freeze == nil {
		return "", ErrAttempt
	}
	if !a.gradingAttempted {
		a.gradingAttempted = true
		body, err := control.Grade(operation, a.source, *a.freeze, a.evidenceSHA, a.worker.deps.executors)
		if err != nil {
			return "", ErrAttempt
		}
		a.terminalBody = bytes.Clone(body)
	}
	if len(a.terminalBody) == 0 {
		return "", ErrAttempt
	}
	return control.PublishTerminal(operation, a.source, a.terminalBody)
}
