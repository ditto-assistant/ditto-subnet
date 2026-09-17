package catalog

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// fixturesPath is the reviewed manifest the approval pins.
const fixturesPath = "../fixtures/preexec/fixtures.json"

func repositoryRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs("../../../../..")
	if err != nil {
		t.Fatalf("repository root: %v", err)
	}
	return root
}

func fixturesBytes(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile(fixturesPath)
	if err != nil {
		t.Fatalf("read fixtures: %v", err)
	}
	return raw
}

// The pinned manifest parses, and every recorded file is the reviewed
// checkout's bytes: the collector stages these and nothing else.
func TestParsePreexecFixturesAcceptsTheReviewedManifest(t *testing.T) {
	fixtures, err := ParsePreexecFixtures(fixturesBytes(t))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if !slices.Equal(slices.Sorted(maps.Keys(fixtures.Languages)), Languages) {
		t.Fatalf("languages = %v", slices.Sorted(maps.Keys(fixtures.Languages)))
	}
	root := repositoryRoot(t)
	files := fixtures.Files()
	if len(files) < 4*(1+len(PreexecControls)) {
		t.Fatalf("files = %d", len(files))
	}
	for _, ref := range files {
		raw, err := os.ReadFile(filepath.Join(root, ref.Path))
		if err != nil {
			t.Fatalf("read %s: %v", ref.Path, err)
		}
		sum := sha256.Sum256(raw)
		if hex.EncodeToString(sum[:]) != ref.SHA256 {
			t.Fatalf("%s digest differs from the manifest", ref.Path)
		}
	}
	for _, language := range Languages {
		entry := fixtures.Languages[language]
		if entry.ExpectedTotal < 2 {
			t.Fatalf("%s expected total = %d", language, entry.ExpectedTotal)
		}
		if entry.TestArgv[0] != TrustedTestDriver {
			t.Fatalf("%s test executable = %s", language, entry.TestArgv[0])
		}
		if len(entry.Hostile) == 0 {
			t.Fatalf("%s has no hostile fixture", language)
		}
		for name, hostile := range entry.Hostile {
			if !strings.HasPrefix(hostile.Subject.Path, fixtureRoot+language+"/hostile/"+name+"/") {
				t.Fatalf("%s hostile %s subject path = %s", language, name, hostile.Subject.Path)
			}
		}
	}
	if fixtures.Languages["go"].Module == nil {
		t.Fatal("go fixtures have no module")
	}
	if fixtures.Languages["rust"].Authority == nil {
		t.Fatal("rust fixtures have no authority")
	}
	if fixtures.Languages["python"].Module != nil || fixtures.Languages["node"].Authority != nil {
		t.Fatal("a language carries another language's key")
	}
}

// Each knob is one property the collector depends on; every one must refuse.
func TestParsePreexecFixturesRefusesTamperedManifests(t *testing.T) {
	for _, testCase := range []struct {
		name string
		turn func(map[string]any)
	}{
		{"schema", func(doc map[string]any) { doc["schema"] = "other-v1" }},
		{"missing language", func(doc map[string]any) {
			delete(doc["languages"].(map[string]any), "rust")
		}},
		{"unknown test group", func(doc map[string]any) {
			language(doc, "go")["test_group"] = "secret"
		}},
		{"expected total below two", func(doc map[string]any) {
			language(doc, "go")["expected_total"] = json.Number("1")
		}},
		{"suite digest differs from the file", func(doc map[string]any) {
			language(doc, "go")["controls_suite_sha256"] = strings.Repeat("a", 64)
		}},
		{"path outside the fixture tree", func(doc map[string]any) {
			control(doc, "go", "pass")["path"] = "infra/scripts/coding-native-evidence.py"
		}},
		{"path of another language", func(doc map[string]any) {
			control(doc, "go", "pass")["path"] = fixtureRoot + "node/controls/pass/subject.js"
		}},
		{"traversing path", func(doc map[string]any) {
			control(doc, "go", "pass")["path"] = fixtureRoot + "go/../../etc/subject.go"
		}},
		{"shell as the test executable", func(doc map[string]any) {
			language(doc, "go")["test_argv"] = []any{"sh", "-c", "true"}
		}},
		{"driver replaced", func(doc map[string]any) {
			argv := language(doc, "go")["test_argv"].([]any)
			argv[0] = "other-driver"
		}},
		{"extra key", func(doc map[string]any) { language(doc, "go")["extra"] = true }},
		{"module on a language that has none", func(doc map[string]any) {
			language(doc, "python")["module"] = control(doc, "go", "pass")
		}},
		{"unknown outcome shape", func(doc map[string]any) {
			hostile(doc, "go", "ptrace")["outcome"] = "Denied"
		}},
		{"hostile name with a path separator", func(doc map[string]any) {
			set := language(doc, "go")["hostile"].(map[string]any)
			set["../escape"] = set["ptrace"]
			delete(set, "ptrace")
		}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			decoder := json.NewDecoder(bytes.NewReader(fixturesBytes(t)))
			decoder.UseNumber()
			var doc map[string]any
			if err := decoder.Decode(&doc); err != nil {
				t.Fatalf("decode: %v", err)
			}
			testCase.turn(doc)
			raw, err := Canonical(doc)
			if err != nil {
				t.Fatalf("canonical: %v", err)
			}
			if _, err := ParsePreexecFixtures(raw); err == nil {
				t.Fatal("the tampered manifest parsed")
			}
		})
	}
}

func language(doc map[string]any, name string) map[string]any {
	return doc["languages"].(map[string]any)[name].(map[string]any)
}

func control(doc map[string]any, name, which string) map[string]any {
	controls := language(doc, name)["controls"].(map[string]any)
	return controls[which].(map[string]any)["subject"].(map[string]any)
}

func hostile(doc map[string]any, name, which string) map[string]any {
	return language(doc, name)["hostile"].(map[string]any)[which].(map[string]any)
}
