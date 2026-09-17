package gen

import (
	"errors"
	"regexp"
)

var privateWords = regexp.MustCompile(`[A-Za-z]+`)

// GeneratePrivateBase records the intentionally misspelled tokens so a
// paraphraser cannot silently undo typo-v2. Observation does not change any
// frozen generator bytes. Returned protection metadata stays producer-side.
func GeneratePrivateBase(seed int64, runSize string, salt uint64) (DatasetArtifact, []string, error) {
	profile, ok := ProfileForVersion(runSize, 13)
	if !ok || salt == 0 || seed < 0 {
		return DatasetArtifact{}, nil, errors.New("private base: invalid identity")
	}
	var tokens []string
	seen := map[string]bool{}
	var invalid bool
	observer := func(before, after string) {
		left, right := privateWords.FindAllString(before, -1), privateWords.FindAllString(after, -1)
		if len(left) != len(right) {
			invalid = true
			return
		}
		for i, word := range right {
			if word != left[i] && !seen[word] {
				tokens = append(tokens, word)
				seen[word] = true
			}
		}
	}
	artifact, err := GenerateDatasetWithSurface(seed, profile, 13, SurfaceOptions{Salt: salt, ObserveTypo: observer})
	if err != nil || invalid {
		return DatasetArtifact{}, nil, errors.New("private base: cannot record typo provenance")
	}
	return artifact, tokens, nil
}
