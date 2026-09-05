package codingexecutor

import (
	"context"
	"errors"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

// NewHostedGrading is the only public constructor that selects native v2
// grading semantics. New remains v1 and cannot infer a version from input.
// All existing Docker isolation, image and trusted-report checks remain active.
func NewHostedGrading(config Config, manifest codinggrader.HostedManifest) (*Executor, error) {
	config.Manifest, config.hosted = codinggrader.Manifest(manifest), true
	return newWithDocker(config, execDocker{})
}

func (factory *PhaseFactory) HostedGrading(ctx context.Context, manifest codinggrader.HostedManifest) (codinggrader.Executor, error) {
	if factory == nil || ctx == nil || ctx.Err() != nil || manifest.Validate(factory.now().UTC()) != nil {
		return nil, errors.New("hosted grading executor authority is invalid")
	}
	return NewHostedGrading(factory.executorConfig(codinggrader.Manifest(manifest), false), manifest)
}

func sameHostedCommand(actual, expected codingrunner.CommandSpec) bool {
	actualSHA, err := commandDigest(actual)
	if err != nil {
		return false
	}
	expectedSHA, err := commandDigest(expected)
	return err == nil && actualSHA == expectedSHA
}

func (executor *Executor) hostedTestAllowed(actual codinggrader.TestGroupSpec) bool {
	for _, expected := range executor.config.Manifest.TestGroups {
		if actual.Group == expected.Group && actual.ExpectedTotal == expected.ExpectedTotal && sameHostedCommand(actual.Command, expected.Command) {
			return true
		}
	}
	return false
}
