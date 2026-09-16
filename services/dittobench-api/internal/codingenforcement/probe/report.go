package probe

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
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

var sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)

// RequestedConfigInspector is the executor's requested-configuration read-back.
type RequestedConfigInspector interface {
	InspectRequestedResourceConfig(ctx context.Context) (codingexecutor.RequestedResourceConfig, error)
}

// HostedGradingRequest selects the approved grading profile and the pinned
// enforcement image set. Image digests and every command come only from
// EnforcementImages, whose grading_profile_sha256 must be GradingProfile's
// digest. Languages optionally narrows the observed languages; it can never
// name an image.
type HostedGradingRequest struct {
	GradingProfile    []byte
	EnforcementImages []byte
	Languages         []string
	Repository        string
	SeccompProfile    string
	AppArmorProfile   string
	Now               func() time.Time
	// RunnerSHA256 measures the running probe binary; RunningExecutableSHA256
	// when nil.
	RunnerSHA256 func() (string, error)
}

// ObserveHostedGradingRequestedConfig inspects the requested resource
// configuration of a hosted grading executor container for each requested
// language image. It refuses a non-rootless or unlabelled daemon, a
// non-canonical or invalid profile, and a missing approved image, and it never
// writes an evidence record.
func ObserveHostedGradingRequestedConfig(ctx context.Context, docker DockerCLI, request HostedGradingRequest) (map[string]any, error) {
	if docker == nil || ctx == nil {
		return nil, errors.New("probe: docker and context are required")
	}
	now := request.Now
	if now == nil {
		now = time.Now
	}
	measure := request.RunnerSHA256
	if measure == nil {
		measure = RunningExecutableSHA256
	}
	runnerSHA256, err := measure()
	if err != nil || !sha256Hex.MatchString(runnerSHA256) {
		return nil, errors.New("probe: the running probe binary could not be measured")
	}
	if err := requireRootlessIsolatedDaemon(ctx, docker); err != nil {
		return nil, err
	}
	profile, err := parseGradingProfile(request.GradingProfile)
	if err != nil {
		return nil, err
	}
	images, err := catalog.ParseEnforcementImages(request.EnforcementImages)
	if err != nil {
		return nil, fmt.Errorf("probe: %w", err)
	}
	profileSum := sha256.Sum256(request.GradingProfile)
	profileSHA256 := hex.EncodeToString(profileSum[:])
	if images.GradingProfileSHA256 != profileSHA256 {
		return nil, errors.New("probe: enforcement images name another grading profile")
	}
	factory, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{
		ImageRepository: request.Repository, CandidateUID: 10001, CandidateGID: 10001,
		RequireRootless: true, RequireIsolatedDaemon: true,
		SeccompProfile: request.SeccompProfile, AppArmorProfile: request.AppArmorProfile, Now: now,
	})
	if err != nil {
		return nil, err
	}
	languages := slices.Clone(catalog.Languages)
	if len(request.Languages) != 0 {
		languages = slices.Clone(request.Languages)
		slices.Sort(languages)
		if len(slices.Compact(slices.Clone(languages))) != len(languages) {
			return nil, errors.New("probe: a language is repeated")
		}
		for _, language := range languages {
			if !slices.Contains(catalog.Languages, language) {
				return nil, fmt.Errorf("probe: unknown language %q", language)
			}
		}
	}
	entries := make([]any, 0, len(languages))
	for _, language := range languages {
		image := images.Images[language]
		resolved, err := ResolveApprovedImage(ctx, docker, request.Repository+"@"+image.ImageDigest)
		if err != nil {
			return nil, fmt.Errorf("probe: %s image: %w", language, err)
		}
		manifest, err := profile.EnforcementProbeManifest(codinghostedworker.EnforcementProbeImage{
			ImageDigest: image.ImageDigest, BuildArgv: image.BuildArgv, TestArgv: image.TestArgv,
		}, now().Add(30*time.Minute))
		if err != nil {
			return nil, fmt.Errorf("probe: %s grading manifest: %w", language, err)
		}
		// The launch digest is the pinned one, not a caller-supplied value.
		if manifest.GraderImageDigest != image.ImageDigest || !strings.HasSuffix(resolved.RepoDigest, "@"+image.ImageDigest) {
			return nil, fmt.Errorf("probe: %s manifest image is not the pinned digest", language)
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
		entry := requestedConfigEntry(language, resolved, requested)
		entry["image_digest"] = image.ImageDigest
		entry["build_argv"] = stringsAny(image.BuildArgv)
		entry["test_argv"] = map[string]any{"hidden": stringsAny(image.TestArgv["hidden"]), "visible": stringsAny(image.TestArgv["visible"])}
		entries = append(entries, entry)
	}
	return map[string]any{
		"schema":               ObservationReportSchema,
		"enforcement_measured": false,
		// Measured on this host from the running process, for comparison
		// with the release-recorded runtime.probe_runner_sha256.
		"probe_runner_binary_sha256": runnerSHA256,
		"grading_profile_sha256":     profileSHA256,
		"enforcement_images_sha256":  images.SHA256,
		"entries":                    entries,
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

func stringsAny(values []string) []any {
	result := make([]any, len(values))
	for index, value := range values {
		result[index] = value
	}
	return result
}

// EncodeReport returns the report's canonical bytes.
func EncodeReport(report map[string]any) ([]byte, error) { return catalog.Canonical(report) }
