package codingexecutor

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
)

// The native enforcement image set pins a Rust test command per group. Its
// offline check (catalog.RustTestArgv, mirrored in the Python evidence tool)
// must accept exactly what this executor's rustCommand accepts for that group.
func TestEnforcementImageRustCommandsMatchTheExecutor(t *testing.T) {
	raw, err := os.ReadFile("../codingenforcement/catalog/testdata/enforcement-images-vector-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var vector struct {
		Refused      map[string]string   `json:"refused"`
		RustTestArgv map[string][]string `json:"rust_test_argv"`
	}
	if err := json.Unmarshal(raw, &vector); err != nil {
		t.Fatal(err)
	}
	executor := func(argv []string, group string) bool {
		named, _, _, err := rustCommand(argv)
		return err == nil && named == group
	}
	checked := 0
	check := func(name string, argv []string, group string) {
		checked++
		if catalog.RustTestArgv(argv, group) != executor(argv, group) {
			t.Errorf("%s: offline check and executor disagree on %v for %s", name, argv, group)
		}
	}
	for group, argv := range vector.RustTestArgv {
		if !executor(argv, group) {
			t.Fatalf("vector %s command is refused by the executor: %v", group, argv)
		}
		check("valid", argv, group)
	}
	refusedRust := 0
	for name, document := range vector.Refused {
		var images struct {
			Images map[string]struct {
				TestArgv map[string][]string `json:"test_argv"`
			} `json:"images"`
		}
		if json.Unmarshal([]byte(document), &images) != nil {
			continue
		}
		for group, argv := range images.Images["rust"].TestArgv {
			check(name, argv, group)
		}
		if strings.HasPrefix(name, "rust ") {
			refusedRust++
			if _, err := catalog.ParseEnforcementImages([]byte(document)); err == nil {
				t.Errorf("%s accepted", name)
			}
		}
	}
	if checked < 20 || refusedRust < 5 {
		t.Fatalf("vector covers too few Rust commands: %d checked, %d refused", checked, refusedRust)
	}
}
