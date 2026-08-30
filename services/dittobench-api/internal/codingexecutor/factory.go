package codingexecutor

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

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
	Now                   func() time.Time
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
		!validProfileName(config.SeccompProfile) || !validProfileName(config.AppArmorProfile) {
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

func (factory *PhaseFactory) executorConfig(manifest codinggrader.Manifest, authoring bool) Config {
	return Config{
		Manifest: manifest, ImageRef: factory.config.ImageRepository + "@" + manifest.GraderImageDigest,
		AuthoringOnly: authoring, SupervisorPath: factory.config.SupervisorPath,
		CandidateUID: factory.config.CandidateUID, CandidateGID: factory.config.CandidateGID,
		RequireRootless:       factory.config.RequireRootless,
		RequireIsolatedDaemon: factory.config.RequireIsolatedDaemon,
		SeccompProfile:        factory.config.SeccompProfile, AppArmorProfile: factory.config.AppArmorProfile,
	}
}

func validImageRepository(value string) bool {
	if value == "" || len(value) > 255 || !utf8.ValidString(value) ||
		strings.HasPrefix(value, "/") || strings.HasPrefix(value, "-") ||
		strings.HasSuffix(value, "/") || strings.Contains(value, "@") ||
		strings.Contains(value, "..") {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) ||
			!(character == '/' || character == '.' || character == '_' || character == '-' ||
				character == ':' || character >= 'a' && character <= 'z' ||
				character >= '0' && character <= '9') {
			return false
		}
	}
	return true
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
