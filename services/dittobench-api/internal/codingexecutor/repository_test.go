package codingexecutor

import (
	"strings"
	"testing"
)

func TestImageRepositoryFollowsTheDockerRepositoryGrammar(t *testing.T) {
	for _, value := range []string{
		"registry.invalid/ditto/coding-runtime",
		"registry.invalid:5000/ditto/coding-runtime",
		"localhost:5000/runtime",
		"ghcr.io/ditto-assistant/dittobench-coding-runtime",
		"runtime",
		"ditto/coding_runtime",
		"ditto/coding__runtime",
		"ditto/coding--runtime",
		"10.0.0.5:5000/ditto.runtime",
		"a/" + strings.Repeat("b", 253),
	} {
		if !validImageRepository(value) {
			t.Errorf("repository %q was rejected", value)
		}
	}
	for _, value := range []string{
		"",
		"registry.invalid//runtime",
		"registry.invalid/a:b/c",
		"registry.invalid/runtime:latest",
		"registry.invalid/runtime@sha256:" + strings.Repeat("1", 64),
		"registry.invalid/runtime/",
		"/registry.invalid/runtime",
		"registry.invalid/../runtime",
		"registry.invalid/run..time",
		"registry.invalid/run___time",
		"registry.invalid/-runtime",
		"registry.invalid/runtime-",
		"Registry.invalid/runtime",
		"registry.invalid/Runtime",
		"registry.invalid/run time",
		"registry.invalid/runtime\n",
		"registry.invalid:/runtime",
		"registry.invalid:123456/runtime",
		"-registry.invalid/runtime",
		"registry-.invalid/runtime",
		"a/" + strings.Repeat("b", 254),
	} {
		if validImageRepository(value) {
			t.Errorf("repository %q was accepted", value)
		}
	}
}
