package gen

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

const MaxPrivateArtifactBytes = 32 << 20

// DecodePrivateArtifact verifies the exact stored bytes against a trusted lease
// pin, then verifies the grading/graph/fixture contract against public datagen.
// The trusted caller must obtain expectedSHA from Platform, not the downloaded
// object's metadata. It must use this returned artifact for execution; validating
// it and then executing a regenerated dataset would defeat the pin.
//
// This is an integrity boundary, NOT semantic or adversarial qualification.
// The producer must have independently validated and qualified the rewrite.
// A decoder failure must never fall back to public generation.
func DecodePrivateArtifact(raw []byte, expectedSHA string, seed int64, runSize string) (DatasetArtifact, error) {
	fail := func(reason string) (DatasetArtifact, error) {
		return DatasetArtifact{}, errors.New("private artifact: " + reason)
	}
	if len(raw) == 0 || len(raw) > MaxPrivateArtifactBytes {
		return fail("invalid size")
	}
	digest := sha256.Sum256(raw)
	if len(expectedSHA) != 64 || hex.EncodeToString(digest[:]) != expectedSHA {
		return fail("lease digest mismatch")
	}
	var artifact DatasetArtifact
	if err := json.Unmarshal(raw, &artifact); err != nil {
		return fail("invalid JSON")
	}
	if artifact.Seed != seed || artifact.BenchVersion != 13 {
		return fail("dataset identity mismatch")
	}
	if recipe := artifact.FactGeneration; recipe != nil {
		if recipe.Revision != V13FactGenerationRevision || artifact.SurfaceSalt != 0 {
			return fail("unsupported fact generation contract")
		}
		replay := universe.NewV13ReplayFactRenderer(recipe.Events)
		base, err := GenerateV13FactDataset(context.Background(), seed, recipe.WorldSeed, recipe.PresentationSeed, runSize, replay)
		if err != nil || replay.Complete() != nil {
			return fail("fact reconstruction failed")
		}
		want, err := base.Marshal()
		got, marshalErr := artifact.Marshal()
		if err != nil || marshalErr != nil || !bytes.Equal(want, got) {
			return fail("fact artifact contract mismatch")
		}
		// Returning regenerated authority preserves JSON-excluded grader rules.
		return base, nil
	}
	if artifact.SurfaceSalt == 0 {
		return fail("missing private generation contract")
	}
	profile, ok := ProfileForVersion(runSize, 13)
	if !ok {
		return fail("unknown profile")
	}
	base, err := GenerateDatasetWithSurface(seed, profile, 13, SurfaceOptions{Salt: artifact.SurfaceSalt})
	if err != nil {
		return fail("cannot verify base contract")
	}
	// Decode again to avoid aliasing the returned surfaces while normalizing
	// the comparison copy. All other fields must be byte-canonically identical.
	var comparison DatasetArtifact
	if err := json.Unmarshal(raw, &comparison); err != nil {
		return fail("invalid comparison")
	}
	if len(comparison.ToolCases) != len(base.ToolCases) || len(comparison.MemoryCases) != len(base.MemoryCases) || len(comparison.MemoryWaves) != len(base.MemoryWaves) {
		return fail("case envelope mismatch")
	}
	for i := range comparison.ToolCases {
		c, b := &comparison.ToolCases[i], &base.ToolCases[i]
		c.Prompt = b.Prompt
		if len(c.PrerequisitePairs) != len(b.PrerequisitePairs) {
			return fail("prerequisite envelope mismatch")
		}
		for j := range c.PrerequisitePairs {
			c.PrerequisitePairs[j].Prompt = b.PrerequisitePairs[j].Prompt
			c.PrerequisitePairs[j].Response = b.PrerequisitePairs[j].Response
		}
	}
	for i := range comparison.MemoryCases {
		comparison.MemoryCases[i].Question = base.MemoryCases[i].Question
	}
	for i := range comparison.MemoryWaves {
		c, b := &comparison.MemoryWaves[i], &base.MemoryWaves[i]
		if len(c.Pairs) != len(b.Pairs) {
			return fail("wave envelope mismatch")
		}
		for j := range c.Pairs {
			c.Pairs[j].Prompt = b.Pairs[j].Prompt
			c.Pairs[j].Response = b.Pairs[j].Response
		}
	}
	before, err := base.Marshal()
	if err != nil {
		return fail("cannot encode base contract")
	}
	normalized, err := comparison.Marshal()
	if err != nil || !bytes.Equal(before, normalized) {
		return fail("immutable contract mismatch")
	}
	// Text normalization above must not erase protected contract values. In
	// particular, old prelaunch artifacts lack the request/record context
	// bindings added to V13. A newly computed object digest cannot bless them.
	protected := v13GlobalProtected(&base)
	sources := map[string]string{}
	if err := visitPrivateSurfaces(&base, func(location string, text *string) error {
		sources[location] = *text
		return nil
	}); err != nil {
		return fail("cannot inspect base surfaces")
	}
	if err := visitPrivateSurfaces(&artifact, func(location string, text *string) error {
		source, ok := sources[location]
		if !ok {
			return errors.New("unknown surface")
		}
		return checkPrivateCandidate(source, *text, protected)
	}); err != nil {
		return fail("protected surface contract mismatch")
	}
	after, err := artifact.Marshal()
	if err != nil || bytes.Equal(before, after) {
		return fail("missing private transformation")
	}
	// JSON deliberately excludes grader-only claims, restraint/twin rules and
	// mutation dependencies. Return the authoritative generated contract with
	// ONLY its text replaced by verified stored surfaces; returning the decoded
	// JSON object would silently discard those grading checks.
	seenPairs := map[string][2]string{}
	copyPair := func(user string, target *protocol.MemoryPair, source protocol.MemoryPair) error {
		if user == "" {
			user = PrimaryUser
		}
		key, _ := json.Marshal([]string{user, source.PairID})
		texts := [2]string{source.Prompt, source.Response}
		if old, ok := seenPairs[string(key)]; ok && old != texts {
			return errors.New("conflicting repeated private surface")
		}
		seenPairs[string(key)] = texts
		target.Prompt, target.Response = source.Prompt, source.Response
		return nil
	}
	for i := range base.ToolCases {
		base.ToolCases[i].Prompt = artifact.ToolCases[i].Prompt
		for j := range base.ToolCases[i].PrerequisitePairs {
			if err := copyPair(PrimaryUser, &base.ToolCases[i].PrerequisitePairs[j], artifact.ToolCases[i].PrerequisitePairs[j]); err != nil {
				return fail(err.Error())
			}
		}
	}
	for i := range base.MemoryCases {
		base.MemoryCases[i].Question = artifact.MemoryCases[i].Question
	}
	for i := range base.MemoryWaves {
		for j := range base.MemoryWaves[i].Pairs {
			if err := copyPair(base.MemoryWaves[i].UserID, &base.MemoryWaves[i].Pairs[j], artifact.MemoryWaves[i].Pairs[j]); err != nil {
				return fail(err.Error())
			}
		}
	}
	return base, nil
}
