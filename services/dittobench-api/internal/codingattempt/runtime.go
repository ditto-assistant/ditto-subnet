package codingattempt

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"reflect"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/google/uuid"
)

var (
	// ErrClosedBeforeFreeze marks integration cleanup that occurred without a
	// successful outer-capability revocation and explicit freeze.
	ErrClosedBeforeFreeze = errors.New("coding authoring session closed before outer capability revocation")
	// ErrFreezeInProgress prevents concurrent cleanup from destroying state
	// while the outer capability is being revoked and the runner is freezing.
	ErrFreezeInProgress = errors.New("coding authoring freeze is in progress")
)

const outerRevocationTimeout = 30 * time.Second

// NewRuntime returns an unwired, fail-closed attempt runtime.
func NewRuntime(config RuntimeConfig) (*Runtime, error) {
	if nilLike(config.Artifacts) || nilLike(config.Executor) {
		return nil, errors.New("coding attempt runtime dependencies are incomplete")
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	return &Runtime{artifacts: config.Artifacts, executor: config.Executor, now: config.Now}, nil
}

// BeginAuthoring verifies authoring-only artifacts, enforces the signed
// resource profile, and constructs one private runner session.
func (runtime *Runtime) BeginAuthoring(ctx context.Context, spec AuthoringSpec) (*AuthoringSession, error) {
	if ctx == nil {
		return nil, errors.New("coding authoring context is required")
	}
	now := runtime.now().UTC()
	if err := validateAuthoringSpec(spec, now); err != nil {
		return nil, err
	}
	resource, err := openArtifact(ctx, runtime.artifacts, spec.ResourceProfile)
	if err != nil {
		return nil, fmt.Errorf("open coding authoring resource profile: %w", err)
	}
	decoded, decodeErr := decodeResourceProfile(resource, spec.ResourceProfileSHA256)
	resourceCloseErr := resource.Close()
	if decodeErr != nil || resourceCloseErr != nil || decoded != spec.ResourcePolicy {
		return nil, errors.Join(errors.New("verify coding authoring resource profile"), decodeErr, resourceCloseErr)
	}
	visible, err := openArtifact(ctx, runtime.artifacts, spec.VisibleBundle)
	if err != nil {
		return nil, fmt.Errorf("open coding authoring visible bundle: %w", err)
	}
	runner, runnerErr := codingrunner.NewSession(ctx, spec.RunnerManifest, visible, runtime.executor)
	visibleCloseErr := visible.Close()
	if runnerErr != nil || visibleCloseErr != nil {
		if runner != nil {
			runnerErr = errors.Join(runnerErr, runner.Close())
		}
		return nil, errors.Join(errors.New("construct coding authoring session"), runnerErr, visibleCloseErr)
	}
	memory, err := openArtifact(ctx, runtime.artifacts, spec.MemoryBundle)
	if err != nil {
		return nil, errors.Join(fmt.Errorf("open coding authoring memory bundle: %w", err), runner.Close())
	}
	return &AuthoringSession{runner: runner, memory: memory}, nil
}

// Freeze revokes the outer route first, then freezes the internal runner and
// closes private memory bytes. Calls after completion return the same cached
// outcome; concurrent lifecycle calls fail without disturbing the owner.
func (session *AuthoringSession) Freeze(
	ctx context.Context,
	revoker CapabilityRevoker,
) (codingrunner.FreezeResult, error) {
	if ctx == nil || nilLike(revoker) {
		return codingrunner.FreezeResult{}, errors.New("coding authoring freeze authority is required")
	}
	session.mu.Lock()
	if session.frozen {
		result, err := cloneFreezeResult(session.freezeResult), session.freezeErr
		session.mu.Unlock()
		return result, err
	}
	if session.freezing {
		session.mu.Unlock()
		return codingrunner.FreezeResult{}, ErrFreezeInProgress
	}
	if session.closed {
		session.mu.Unlock()
		return codingrunner.FreezeResult{}, errors.New("coding authoring session is closed")
	}
	session.freezing = true
	session.mu.Unlock()

	revokeContext, cancelRevoke := context.WithTimeout(context.WithoutCancel(ctx), outerRevocationTimeout)
	revokeErr := revoker.Revoke(revokeContext)
	cancelRevoke()
	result := session.runner.Freeze()
	memoryErr := session.memory.Close()

	session.mu.Lock()
	session.freezing = false
	session.memoryClosed = true
	session.frozen = true
	session.freezeResult = cloneFreezeResult(result)
	session.freezeErr = errors.Join(revokeErr, memoryErr)
	cached, freezeErr := cloneFreezeResult(session.freezeResult), session.freezeErr
	session.mu.Unlock()
	return cached, freezeErr
}

// WriteTranscript streams the exact runner transcript after freeze.
func (session *AuthoringSession) WriteTranscript(destination io.Writer) (codingrunner.TranscriptIdentity, error) {
	if destination == nil {
		return codingrunner.TranscriptIdentity{}, errors.New("coding transcript destination is required")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !session.frozen {
		return codingrunner.TranscriptIdentity{}, errors.New("coding transcript requires a frozen session")
	}
	return session.runner.WriteTranscript(destination)
}

// Close destroys local authoring state. Closing before Freeze is an explicit
// integration error because the runtime cannot revoke an unknown outer route.
func (session *AuthoringSession) Close() error {
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.closed {
		return session.closeErr
	}
	if session.freezing {
		return ErrFreezeInProgress
	}
	session.closed = true
	premature := !session.frozen
	var memoryErr error
	if !session.memoryClosed {
		memoryErr = session.memory.Close()
		session.memoryClosed = true
	}
	err := errors.Join(memoryErr, session.runner.Close())
	if premature {
		err = errors.Join(ErrClosedBeforeFreeze, err)
	}
	session.closeErr = err
	return session.closeErr
}

// Grade verifies grading-only artifacts, revalidates the resource profile,
// and delegates to the existing pristine grader with no authoring workspace.
func (runtime *Runtime) Grade(
	ctx context.Context,
	spec GradingSpec,
	submission codingrunner.FrozenSubmission,
) (codinggrader.Result, error) {
	if ctx == nil {
		return codinggrader.Result{}, errors.New("coding grading context is required")
	}
	now := runtime.now().UTC()
	if err := validateGradingSpec(spec, submission, now); err != nil {
		return codinggrader.Result{}, err
	}
	resource, err := openArtifact(ctx, runtime.artifacts, spec.ResourceProfile)
	if err != nil {
		return codinggrader.Result{}, fmt.Errorf("open coding grading resource profile: %w", err)
	}
	decoded, decodeErr := decodeResourceProfile(resource, spec.GraderManifest.ResourceProfileSHA256)
	resourceCloseErr := resource.Close()
	if decodeErr != nil || resourceCloseErr != nil || decoded != spec.GraderManifest.ResourcePolicy {
		return codinggrader.Result{}, errors.Join(errors.New("verify coding grading resource profile"), decodeErr, resourceCloseErr)
	}
	visible, err := openArtifact(ctx, runtime.artifacts, spec.VisibleBundle)
	if err != nil {
		return codinggrader.Result{}, fmt.Errorf("open coding grading visible bundle: %w", err)
	}
	result := codinggrader.GradeWithProtectedOpener(
		ctx,
		spec.GraderManifest,
		submission,
		visible,
		func(openContext context.Context) (io.ReadCloser, error) {
			grader, openErr := openArtifact(openContext, runtime.artifacts, spec.GraderBundle)
			if openErr != nil {
				return nil, fmt.Errorf("open coding protected grader bundle: %w", openErr)
			}
			return grader, nil
		},
		runtime.executor,
	)
	if closeErr := visible.Close(); closeErr != nil {
		return codinggrader.Result{}, errors.Join(errors.New("close coding grading artifacts"), closeErr)
	}
	return result, nil
}

func openArtifact(
	ctx context.Context,
	source ArtifactSource,
	capability codingartifacts.Capability,
) (io.ReadCloser, error) {
	reader, err := source.Open(ctx, capability)
	if err != nil {
		if !nilLike(reader) {
			err = errors.Join(err, reader.Close())
		}
		return nil, err
	}
	if nilLike(reader) {
		return nil, errors.New("coding artifact source returned no reader")
	}
	return reader, nil
}

func nilLike(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func validateAuthoringSpec(spec AuthoringSpec, now time.Time) error {
	if err := validateBinding(spec.Binding, now); err != nil {
		return err
	}
	if err := spec.RunnerManifest.Validate(now); err != nil {
		return fmt.Errorf("coding authoring runner manifest: %w", err)
	}
	resourceSHA, err := codinggrader.ResourceProfileSHA256(spec.ResourcePolicy)
	if err != nil || spec.ResourcePolicy.Validate() != nil || resourceSHA != spec.ResourceProfileSHA256 ||
		spec.RunnerManifest.Limits != spec.ResourcePolicy.CandidateLimits || !lowerSHA256(spec.MemoryBundleSHA256) {
		return errors.New("coding authoring resource authority is invalid")
	}
	if spec.RunnerManifest.TicketID != spec.Binding.TicketID || spec.RunnerManifest.CaseID != spec.Binding.CaseID ||
		spec.RunnerManifest.ProfileCapabilityID != spec.Binding.ProfileCapabilityID ||
		!spec.RunnerManifest.Deadline.Equal(spec.Binding.Deadline) {
		return errors.New("coding authoring manifest identity is invalid")
	}
	if err := validateCapability(spec.VisibleBundle, spec.Binding, codingartifacts.PhaseAuthoring,
		codingartifacts.KindVisibleBundle, codingartifacts.AudienceWorkspaceMaterializer,
		spec.RunnerManifest.VisibleBundleSHA256, now); err != nil {
		return err
	}
	if err := validateCapability(spec.MemoryBundle, spec.Binding, codingartifacts.PhaseAuthoring,
		codingartifacts.KindMemoryBundle, codingartifacts.AudienceMemorySeedProjector, spec.MemoryBundleSHA256, now); err != nil {
		return err
	}
	return validateCapability(spec.ResourceProfile, spec.Binding, codingartifacts.PhaseAuthoring,
		codingartifacts.KindResourceProfile, codingartifacts.AudienceResourceSupervisor, spec.ResourceProfileSHA256, now)
}

func validateGradingSpec(spec GradingSpec, submission codingrunner.FrozenSubmission, now time.Time) error {
	if err := validateBinding(spec.Binding, now); err != nil {
		return err
	}
	freezeID, freezeErr := uuid.Parse(spec.FreezeID)
	if freezeErr != nil || freezeID == uuid.Nil || !lowerSHA256(spec.AuthoringEvidenceSHA256) ||
		!lowerSHA256(spec.FrozenPatchSHA256) ||
		spec.FrozenSubmissionKey != "sha256/"+spec.FrozenPatchSHA256 ||
		submission.FrozenPatchSHA256 != spec.FrozenPatchSHA256 ||
		submission.AuthoringTranscriptBytes <= 0 || len(submission.ChangedPaths) == 0 ||
		!submission.ProtectedPathsIntact {
		return errors.New("coding grading freeze authority is invalid")
	}
	if err := spec.GraderManifest.Validate(now); err != nil {
		return fmt.Errorf("coding grading manifest: %w", err)
	}
	if err := codingrunner.ValidateFrozenSubmission(
		submission,
		spec.GraderManifest.ResourcePolicy.CandidateLimits,
	); err != nil {
		return errors.New("coding grading frozen submission is invalid")
	}
	if spec.GraderManifest.CaseID != spec.Binding.CaseID || !spec.GraderManifest.Deadline.Equal(spec.Binding.Deadline) {
		return errors.New("coding grading manifest identity is invalid")
	}
	if err := validateCapability(spec.VisibleBundle, spec.Binding, codingartifacts.PhaseGrading,
		codingartifacts.KindVisibleBundle, codingartifacts.AudienceWorkspaceMaterializer,
		spec.GraderManifest.VisibleBundleSHA256, now); err != nil {
		return err
	}
	if err := validateCapability(spec.ResourceProfile, spec.Binding, codingartifacts.PhaseGrading,
		codingartifacts.KindResourceProfile, codingartifacts.AudienceResourceSupervisor,
		spec.GraderManifest.ResourceProfileSHA256, now); err != nil {
		return err
	}
	return validateCapability(spec.GraderBundle, spec.Binding, codingartifacts.PhaseGrading,
		codingartifacts.KindGraderBundle, codingartifacts.AudienceProtectedGrader,
		spec.GraderManifest.GraderBundleSHA256, now)
}

func validateBinding(binding Binding, now time.Time) error {
	ticketID, ticketErr := uuid.Parse(binding.TicketID)
	if ticketErr != nil || ticketID == uuid.Nil || !validIdentifier(binding.CaseID, 256) ||
		!validIdentifier(binding.ProfileCapabilityID, 256) || binding.Deadline.IsZero() ||
		!binding.Deadline.After(now) || binding.Deadline.After(now.Add(2*time.Hour)) {
		return errors.New("coding attempt binding is invalid")
	}
	return nil
}

func validateCapability(
	capability codingartifacts.Capability,
	binding Binding,
	phase codingartifacts.DeliveryPhase,
	kind codingartifacts.Kind,
	audience codingartifacts.Audience,
	digest string,
	now time.Time,
) error {
	if capability.TicketID != binding.TicketID || !capability.TicketDeadline.Equal(binding.Deadline) ||
		capability.Phase != phase || capability.Kind != kind || capability.Audience != audience ||
		capability.SHA256 != digest || capability.SizeBytes <= 0 || capability.URL == "" ||
		!capability.ExpiresAt.After(now) ||
		capability.ExpiresAt.After(binding.Deadline) {
		return errors.New("coding attempt artifact capability is invalid")
	}
	return nil
}

func cloneFreezeResult(result codingrunner.FreezeResult) codingrunner.FreezeResult {
	if result.Submission != nil {
		clone := *result.Submission
		clone.ChangedPaths = cloneStrings(clone.ChangedPaths)
		clone.Changes = make([]codingrunner.FrozenChange, len(result.Submission.Changes))
		for index, change := range result.Submission.Changes {
			clone.Changes[index] = change
			clone.Changes[index].AfterContent = cloneBytes(change.AfterContent)
			if change.BeforeSHA256 != nil {
				value := *change.BeforeSHA256
				clone.Changes[index].BeforeSHA256 = &value
			}
			if change.AfterSHA256 != nil {
				value := *change.AfterSHA256
				clone.Changes[index].AfterSHA256 = &value
			}
		}
		clone.Patch = cloneBytes(clone.Patch)
		result.Submission = &clone
	}
	if result.Failure != nil {
		clone := *result.Failure
		result.Failure = &clone
	}
	return result
}

func cloneStrings(values []string) []string {
	if values == nil {
		return nil
	}
	result := make([]string, len(values))
	copy(result, values)
	return result
}

func cloneBytes(values []byte) []byte {
	if values == nil {
		return nil
	}
	result := make([]byte, len(values))
	copy(result, values)
	return result
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func lowerSHA256(value string) bool {
	if len(value) != 64 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
