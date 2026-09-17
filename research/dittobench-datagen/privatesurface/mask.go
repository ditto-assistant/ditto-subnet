package privatesurface

import (
	"errors"
	"fmt"
	"sort"
	"strings"
)

// Masking protects exact values and intentionally corrupted tokens from a
// model's spelling normalization. It is not a semantic validator: ordering and
// relationships still need independent validation after restoring the tokens.
func maskProtected(source string, protected []string) (string, []string, func(string) (string, error), error) {
	prefix := "⟦v13_" + digest([]byte(source))[:16] + "_"
	if strings.Contains(source, prefix) {
		return "", nil, nil, errors.New("private producer: mask collision")
	}
	seen := map[string]bool{}
	var values []string
	for _, value := range protected {
		if value != "" && !seen[value] && strings.Contains(source, value) {
			values = append(values, value)
			seen[value] = true
		}
	}
	sort.Slice(values, func(i, j int) bool {
		if len(values[i]) != len(values[j]) {
			return len(values[i]) > len(values[j])
		}
		return values[i] < values[j]
	})
	var pairs, reverse, markers []string
	for i, value := range values {
		marker := fmt.Sprintf("%s%d⟧", prefix, i)
		pairs = append(pairs, value, marker)
		reverse = append(reverse, marker, value)
		markers = append(markers, marker)
	}
	masked := strings.NewReplacer(pairs...).Replace(source)
	used := make([]string, 0, len(markers))
	for _, marker := range markers {
		if strings.Contains(masked, marker) {
			used = append(used, marker)
		}
	}
	restore := func(candidate string) (string, error) {
		for _, marker := range markers {
			if strings.Count(candidate, marker) != strings.Count(masked, marker) {
				return "", errors.New("private producer: protected marker changed")
			}
		}
		after := strings.NewReplacer(reverse...).Replace(candidate)
		if strings.Contains(after, prefix) {
			return "", errors.New("private producer: unknown marker introduced")
		}
		for _, value := range values {
			if strings.Count(after, value) != strings.Count(source, value) {
				return "", errors.New("private producer: protected value changed")
			}
		}
		return after, nil
	}
	return masked, used, restore, nil
}
