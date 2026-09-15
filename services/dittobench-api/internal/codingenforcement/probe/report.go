package probe

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedworker"
)

// ObservationReportSchema names the runner's only output. It is deliberately
// not the evidence record schema, and the offline verifier refuses it.
const ObservationReportSchema = "dittobench-coding-native-probe-observations-v1"

// requestedConfigSource says exactly what was observed.
const requestedConfigSource = "docker_inspect_created_unstarted_container"

// RequestedConfigInspector is the executor's requested-configuration read-back.
type RequestedConfigInspector interface {
	InspectRequestedResourceConfig(ctx context.Context) (codingexecutor.RequestedResourceConfig, error)
}

// HostedGradingRequest selects the approved grading profile and images to
// inspect. Images maps a catalog language to its approved sha256 digest in
// Repository.
type HostedGradingRequest struct {
	GradingProfile  []byte
	Repository      string
	Images          map[string]string
	SeccompProfile  string
	AppArmorProfile string
	Now             func() time.Time
}

// ObserveHostedGradingRequestedConfig inspects the requested resource
// configuration of a hosted grading executor container for each requested
// language image. It refuses a non-rootless or unlabelled daemon, a
// non-canonical or invalid profile, and a missing approved image, and it never
// writes an evidence record.
func ObserveHostedGradingRequestedConfig(ctx context.Context, docker DockerCLI, request HostedGradingRequest) (map[string]any, error) {
	if docker == nil || ctx == nil || len(request.Images) == 0 {
		return nil, errors.New("probe: docker, context and at least one image are required")
	}
	now := request.Now
	if now == nil {
		now = time.Now
	}
	if err := requireRootlessIsolatedDaemon(ctx, docker); err != nil {
		return nil, err
	}
	profile, err := parseGradingProfile(request.GradingProfile)
	if err != nil {
		return nil, err
	}
	factory, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{
		ImageRepository: request.Repository, CandidateUID: 10001, CandidateGID: 10001,
		RequireRootless: true, RequireIsolatedDaemon: true,
		SeccompProfile: request.SeccompProfile, AppArmorProfile: request.AppArmorProfile, Now: now,
	})
	if err != nil {
		return nil, err
	}
	languages := make([]string, 0, len(request.Images))
	for language := range request.Images {
		if !slices.Contains(catalog.Languages, language) {
			return nil, fmt.Errorf("probe: unknown language %q", language)
		}
		languages = append(languages, language)
	}
	slices.Sort(languages)
	entries := make([]any, 0, len(languages))
	for _, language := range languages {
		digest := request.Images[language]
		resolved, err := ResolveApprovedImage(ctx, docker, request.Repository+"@"+digest)
		if err != nil {
			return nil, fmt.Errorf("probe: %s image: %w", language, err)
		}
		manifest, err := profile.EnforcementProbeManifest(digest, now().Add(30*time.Minute))
		if err != nil {
			return nil, fmt.Errorf("probe: %s grading manifest: %w", language, err)
		}
		grading, err := factory.HostedGrading(ctx, manifest)
		if err != nil {
			return nil, fmt.Errorf("probe: %s hosted grading executor: %w", language, err)
		}
		inspector, ok := grading.(RequestedConfigInspector)
		if !ok {
			return nil, errors.New("probe: hosted grading executor cannot inspect requested configuration")
		}
		requested, err := inspector.InspectRequestedResourceConfig(ctx)
		if err != nil {
			return nil, fmt.Errorf("probe: %s requested configuration: %w", language, err)
		}
		entries = append(entries, requestedConfigEntry(language, resolved, requested))
	}
	return map[string]any{
		"schema":               ObservationReportSchema,
		"enforcement_measured": false,
		"entries":              entries,
	}, nil
}

func requestedConfigEntry(language string, resolved ResolvedImage, requested codingexecutor.RequestedResourceConfig) map[string]any {
	return map[string]any{
		"container_class":   "executor_grading",
		"language":          language,
		"image_repo_digest": resolved.RepoDigest,
		"image_id":          resolved.ID,
		"source":            requestedConfigSource,
		"requested_config": map[string]any{
			"memory_limit_bytes":  requested.MemoryLimitBytes,
			"memory_swap_bytes":   requested.MemorySwapBytes,
			"cpu_quota_millis":    requested.NanoCPUs / 1_000_000,
			"pids_limit":          requested.PidsLimit,
			"scratch_limit_bytes": requested.ScratchLimitBytes,
			"read_only_rootfs":    requested.ReadonlyRootfs,
		},
	}
}

// parseGradingProfile accepts only the exact canonical approved profile bytes
// the hosted runtime accepts.
func parseGradingProfile(raw []byte) (codinghostedworker.GradingProfile, error) {
	var profile codinghostedworker.GradingProfile
	if codingcontract.ValidateJSONDocument(raw, 64<<10) != nil || codingcontract.RequireExactCanonicalJSON(raw) != nil {
		return profile, errors.New("probe: grading profile is not exact canonical JSON")
	}
	if err := json.Unmarshal(raw, &profile); err != nil || profile.Validate() != nil {
		return profile, errors.New("probe: grading profile is invalid")
	}
	return profile, nil
}

// EncodeReport returns the report's canonical bytes.
func EncodeReport(report map[string]any) ([]byte, error) { return catalog.Canonical(report) }
