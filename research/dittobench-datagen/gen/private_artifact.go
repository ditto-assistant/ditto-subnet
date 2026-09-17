package gen

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
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
	if artifact.Seed != seed || artifact.BenchVersion != 13 || artifact.SurfaceSalt == 0 {
		return fail("dataset identity mismatch")
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
