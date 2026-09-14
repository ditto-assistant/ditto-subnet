package gen

import (
	"os"
	"regexp"
	"strings"
	"testing"
)

// v13PublishedKnownVector is the value TestV13KnownVector pins; the README
// known-vector table and docs/bench-versions.md must publish the same string.
const v13PublishedKnownVector = "ef438df9b92ac7c9a19d92ed97dba1d01efdcc2f42936d5f505e23cc71a4f202"

// TestV13KnownVectorIsPublishedConsistently keeps the published v13 vector
// from drifting away from the pinned test constant: the README row for
// version 13 and the docs/bench-versions.md pin both carry exactly the value
// TestV13KnownVector asserts, and neither still calls it a placeholder. An
// auditor regenerating v13 from the published seed must get the published hash.
func TestV13KnownVectorIsPublishedConsistently(t *testing.T) {
	if v13PublishedKnownVector != v13KnownVectorWant {
		t.Fatalf("docs constant %s != TestV13KnownVector pin %s", v13PublishedKnownVector, v13KnownVectorWant)
	}
	readme, err := os.ReadFile("../README.md")
	if err != nil {
		t.Fatal(err)
	}
	row := regexp.MustCompile(`(?m)^\| 13[^|]*\|[^|]*\|\s*` + "`" + `([0-9a-f]{64})` + "`" + `\s*\|`)
	match := row.FindStringSubmatch(string(readme))
	if match == nil {
		t.Fatal("README known-vector table has no version 13 row")
	}
	if match[1] != v13PublishedKnownVector {
		t.Fatalf("README v13 row publishes %s, test pins %s", match[1], v13PublishedKnownVector)
	}
	if strings.Contains(match[0], "placeholder") {
		t.Fatalf("README v13 row still says placeholder: %s", match[0])
	}
	docs, err := os.ReadFile("../docs/bench-versions.md")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(docs), v13PublishedKnownVector) {
		t.Fatal("docs/bench-versions.md does not publish the pinned v13 vector")
	}
	for _, stale := range regexp.MustCompile(`[0-9a-f]{64}`).FindAllString(string(docs), -1) {
		if stale != v13PublishedKnownVector && strings.Contains(string(docs), "TestV13KnownVector") && stale == "b9bfb611f4509599fb6c79579114244178ca09077737a0313b6db9c5b6f1966c" {
			t.Fatalf("docs/bench-versions.md still publishes the plumbing placeholder %s", stale)
		}
	}
}
