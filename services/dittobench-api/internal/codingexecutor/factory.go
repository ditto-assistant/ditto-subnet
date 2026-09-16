package codingexecutor

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"regexp"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingattempt"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

// FactoryConfig is host-owned executor authority shared by both coding phases.
// The immutable image digest and phase-specific plan always come from the
// verified lease, never from this configuration.
type FactoryConfig struct {
	ImageRepository       string
	SupervisorPath        string
	CandidateUID          uint32
	CandidateGID          uint32
	RequireRootless       bool
	RequireIsolatedDaemon bool
	SeccompProfile        string
	AppArmorProfile       string
	// DockerHost selects the dedicated coding daemon for every executor this
	// factory creates. Empty inherits the process DOCKER_HOST.
	DockerHost string
	Now        func() time.Time
	// LaunchIntent is passed to every executor this factory creates.
	LaunchIntent LaunchIntent
}

// PhaseFactory creates a fresh executor after each phase has verified its own
// authority. It keeps no task, image digest, command, or workspace state.
type PhaseFactory struct {
	config FactoryConfig
	now    func() time.Time
}

func NewPhaseFactory(config FactoryConfig) (*PhaseFactory, error) {
	if !validImageRepository(config.ImageRepository) ||
		config.CandidateUID == 0 || config.CandidateGID == 0 ||
		!config.RequireRootless || !config.RequireIsolatedDaemon ||
		!validProfileName(config.SeccompProfile) || !validProfileName(config.AppArmorProfile) ||
		(config.DockerHost != "" && !ValidDedicatedDockerHost(config.DockerHost)) {
		return nil, errors.New("coding executor factory configuration is invalid")
	}
	if config.SupervisorPath == "" {
		config.SupervisorPath = defaultSupervisorPath
	}
	if config.SupervisorPath != defaultSupervisorPath {
		return nil, errors.New("coding executor factory supervisor is invalid")
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	return &PhaseFactory{config: config, now: config.Now}, nil
}

func (factory *PhaseFactory) Authoring(
	ctx context.Context,
	imageDigest string,
	policy codinggrader.ResourcePolicy,
) (codingrunner.CommandExecutor, error) {
	if factory == nil || ctx == nil || ctx.Err() != nil ||
		!ociDigest(imageDigest) || policy.Validate() != nil {
		return nil, errors.New("coding authoring executor authority is invalid")
	}
	return New(factory.executorConfig(codinggrader.Manifest{
		GraderImageDigest: imageDigest,
		GraderPlatform:    "linux/amd64",
		ResourcePolicy:    policy,
	}, true))
}

func (factory *PhaseFactory) Grading(
	ctx context.Context,
	manifest codinggrader.Manifest,
) (codinggrader.Executor, error) {
	if factory == nil || ctx == nil || ctx.Err() != nil || manifest.Validate(factory.now().UTC()) != nil {
		return nil, errors.New("coding grading executor authority is invalid")
	}
	return New(factory.executorConfig(manifest, false))
}

func (factory *PhaseFactory) Certification(
	ctx context.Context,
	manifest codinggrader.Manifest,
) (*Executor, error) {
	if factory == nil || ctx == nil || ctx.Err() != nil || manifest.Validate(factory.now().UTC()) != nil {
		return nil, errors.New("coding certification executor authority is invalid")
	}
	return New(factory.executorConfig(manifest, false))
}

func (factory *PhaseFactory) executorConfig(manifest codinggrader.Manifest, authoring bool) Config {
	return Config{
		Manifest: manifest, ImageRef: factory.config.ImageRepository + "@" + manifest.GraderImageDigest,
		AuthoringOnly: authoring, SupervisorPath: factory.config.SupervisorPath,
		CandidateUID: factory.config.CandidateUID, CandidateGID: factory.config.CandidateGID,
		RequireRootless:       factory.config.RequireRootless,
		RequireIsolatedDaemon: factory.config.RequireIsolatedDaemon,
		SeccompProfile:        factory.config.SeccompProfile, AppArmorProfile: factory.config.AppArmorProfile,
		DockerHost:   factory.config.DockerHost,
		LaunchIntent: factory.config.LaunchIntent,
	}
}

// CertificationReadiness reports, without creating a container, whether a
// certification executor could pass its Docker preflight right now: the
// configured endpoint is a rootless daemon with the isolated-daemon label, and
// the exact runtime image digest is present locally with the supervisor
// contract. It is advisory; every executor still runs the full preflight.
func (factory *PhaseFactory) CertificationReadiness(ctx context.Context, imageDigest string) (daemon bool, image bool) {
	if factory == nil {
		return false, false
	}
	return factory.certificationReadiness(ctx, execDocker{host: factory.config.DockerHost}, imageDigest)
}

func (factory *PhaseFactory) certificationReadiness(
	ctx context.Context,
	docker dockerCLI,
	imageDigest string,
) (daemon bool, image bool) {
	if factory == nil || ctx == nil || ctx.Err() != nil || docker == nil || !ociDigest(imageDigest) {
		return false, false
	}
	if verifyRootlessIsolatedDaemon(ctx, docker) != nil {
		return false, false
	}
	_, err := inspectSupervisorImage(
		ctx, docker, factory.config.ImageRepository+"@"+imageDigest, imageDigest, "linux/amd64", false,
	)
	return true, err == nil
}

// dockerRepositoryName is the Docker reference grammar for a repository name,
// restricted to lowercase: an optional registry host[:port] component followed
// by one or more path components separated by single slashes. A tag or digest
// is never part of the repository; the executor appends the verified digest.
var dockerRepositoryName = regexp.MustCompile(
	`^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?/)?` +
		`[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$`,
)

func validImageRepository(value string) bool {
	return len(value) <= 255 && dockerRepositoryName.MatchString(value)
}

func (factory *PhaseFactory) String() string   { return "CodingExecutorPhaseFactory{private}" }
func (factory *PhaseFactory) GoString() string { return factory.String() }
func (factory *PhaseFactory) LogValue() slog.Value {
	return slog.StringValue("coding-executor-phase-factory")
}
func (*PhaseFactory) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding executor factory is private")
}

var _ codingattempt.ExecutorFactory = (*PhaseFactory)(nil)
var _ json.Marshaler = (*PhaseFactory)(nil)
