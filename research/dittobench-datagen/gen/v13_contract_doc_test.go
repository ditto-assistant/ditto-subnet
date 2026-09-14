package gen

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// The Bench v13 contract is published in four documents. This test keeps them
// honest against each other and against the tree: every public rule a v13
// harness can be scored on has a statement in the datagen contract AND in the
// wire protocol, each statement points at the vector test that pins it, and
// every vector test the docs name either exists in the tree or is explicitly
// attributed to the issue whose PR carries it into the stack (`(#NNNN)` on the
// same or the following line, so a wrapped sentence still counts). A renamed
// test, a rule documented without a vector, or a conflict marker left by a
// stack merge fails here instead of at review.

// v13 contract documents, relative to this package directory. The first is
// module-local; the rest exist only in the monorepo checkout.
const (
	v13ContractDoc         = "../docs/bench-versions.md"
	v13ProjectionDoc       = "../docs/v9-harness-projection.md"
	v13WireProtocolDoc     = "../../../services/dittobench-api/PROTOCOL.md"
	v13StarterProtocolDoc  = "../../../miners/dittobench-starter-kit/PROTOCOL.md"
	v13StarterReadmeDoc    = "../../../miners/dittobench-starter-kit/README.md"
	v13ContractSectionHead = "## Bench v13 (private, typed-semantic contract)"
	v13WireSectionHead     = "### Harness wire version for Bench v10 and later"
	v13WireSectionEnd      = "## Anti-copy signals"
)

// v13PublicRules is every scorer- or grader-visible v13 rule the issue #1853
// acceptance list names. Each token must appear (case-insensitively) in both
// contract documents, inside a heading block that also names its vector.
var v13PublicRules = []string{
	"restraint_without_offer",
	"expected_tool_not_offered",
	"swallowed_model_call",
	"semantic-preloading safe harbor",
	"served_text_not_model_emitted",
	"slot_not_in_prose",
	"answer_in_prompt",
	"twin_concordant",
	"counterfactual_insensitive",
	"inference_cost",
	"`enum`",
	"decoy",
	"ingest acknowledgement",
	"as_of_twin",
	"question's language",
}

var (
	v13TestRefPattern = regexp.MustCompile(`\bTest[A-Z][A-Za-z0-9_]*`)
	v13IssueMarker    = regexp.MustCompile(`#\d{4}\b`)
	v13VectorPointer  = regexp.MustCompile(`\bTest[A-Z][A-Za-z0-9_]*|_test\.go\b`)
	v13ConflictMarker = regexp.MustCompile(`(?m)^(<<<<<<<|=======|>>>>>>>)`)
)

func readV13Doc(t *testing.T, rel string, monorepoOnly bool) string {
	t.Helper()
	body, err := os.ReadFile(rel)
	if err != nil {
		if monorepoOnly && os.IsNotExist(err) {
			t.Skipf("%s is only present in the monorepo checkout: %v", rel, err)
		}
		t.Fatalf("read %s: %v", rel, err)
	}
	return string(body)
}

// sectionBetween returns the text from the first line starting with head up to
// (excluding) the next line that starts with end. An empty end means "the next
// heading of the same level as head".
func sectionBetween(t *testing.T, doc, head, end string) string {
	t.Helper()
	start := strings.Index(doc, head)
	if start < 0 {
		t.Fatalf("heading %q not found", head)
	}
	rest := doc[start+len(head):]
	if end == "" {
		level := strings.SplitN(head, " ", 2)[0] + " "
		if i := strings.Index(rest, "\n"+level); i >= 0 {
			return head + rest[:i]
		}
		return head + rest
	}
	if i := strings.Index(rest, end); i >= 0 {
		return head + rest[:i]
	}
	t.Fatalf("section end %q not found after %q", end, head)
	return ""
}

// headingBlocks splits markdown into blocks, one per `#`-heading.
func headingBlocks(section string) []string {
	var blocks []string
	var current strings.Builder
	for _, line := range strings.Split(section, "\n") {
		if strings.HasPrefix(line, "#") && current.Len() > 0 {
			blocks = append(blocks, current.String())
			current.Reset()
		}
		current.WriteString(line)
		current.WriteByte('\n')
	}
	if current.Len() > 0 {
		blocks = append(blocks, current.String())
	}
	return blocks
}

// goTestFuncsInTree walks every non-vendored Go file under the roots and
// returns the declared top-level function names.
func goTestFuncsInTree(t *testing.T, roots ...string) map[string]bool {
	t.Helper()
	funcs := map[string]bool{}
	decl := regexp.MustCompile(`(?m)^func (Test[A-Z][A-Za-z0-9_]*)\(`)
	for _, root := range roots {
		if _, err := os.Stat(root); err != nil {
			continue
		}
		err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				switch d.Name() {
				case "vendor", "node_modules", ".git", "target":
					return filepath.SkipDir
				}
				return nil
			}
			if !strings.HasSuffix(path, "_test.go") {
				return nil
			}
			body, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			for _, m := range decl.FindAllStringSubmatch(string(body), -1) {
				funcs[m[1]] = true
			}
			return nil
		})
		if err != nil {
			t.Fatalf("walk %s: %v", root, err)
		}
	}
	return funcs
}

func TestV13ContractDocCoversEveryPublicRuleWithAVector(t *testing.T) {
	contract := sectionBetween(t, readV13Doc(t, v13ContractDoc, false), v13ContractSectionHead, "")
	wire := sectionBetween(t, readV13Doc(t, v13WireProtocolDoc, true), v13WireSectionHead, v13WireSectionEnd)

	for name, section := range map[string]string{"bench-versions.md": contract, "PROTOCOL.md": wire} {
		blocks := headingBlocks(section)
		for _, rule := range v13PublicRules {
			stated, vectored := false, false
			for _, block := range blocks {
				if !strings.Contains(strings.ToLower(block), strings.ToLower(rule)) {
					continue
				}
				stated = true
				if v13VectorPointer.MatchString(block) {
					vectored = true
					break
				}
			}
			if !stated {
				t.Errorf("%s: v13 rule %q has no documented statement", name, rule)
			} else if !vectored {
				t.Errorf("%s: v13 rule %q is stated but no block stating it names a vector test", name, rule)
			}
		}
	}
}

func TestV13ContractDocVectorReferencesExistOrNameTheirIssue(t *testing.T) {
	docs := map[string]string{
		v13ContractDoc:        sectionBetween(t, readV13Doc(t, v13ContractDoc, false), v13ContractSectionHead, ""),
		v13ProjectionDoc:      readV13Doc(t, v13ProjectionDoc, false),
		v13WireProtocolDoc:    sectionBetween(t, readV13Doc(t, v13WireProtocolDoc, true), v13WireSectionHead, v13WireSectionEnd),
		v13StarterProtocolDoc: readV13Doc(t, v13StarterProtocolDoc, true),
		v13StarterReadmeDoc:   readV13Doc(t, v13StarterReadmeDoc, true),
	}
	known := goTestFuncsInTree(t, "..", "../../../services", "../../../workers")
	if !known["TestV12KnownVector"] {
		t.Fatalf("tree walk did not find TestV12KnownVector; the reference scan is not looking at the tree")
	}
	seen := 0
	for path, body := range docs {
		lines := strings.Split(body, "\n")
		for lineNo, line := range lines {
			for _, ref := range v13TestRefPattern.FindAllString(line, -1) {
				seen++
				if known[ref] {
					continue
				}
				window := line
				if lineNo+1 < len(lines) {
					window += "\n" + lines[lineNo+1]
				}
				if v13IssueMarker.MatchString(window) {
					continue
				}
				t.Errorf("%s:%d names %s, which is not declared in the tree and carries no `#NNNN` issue marker on its line or the next", path, lineNo+1, ref)
			}
		}
	}
	if seen == 0 {
		t.Fatalf("no vector test references found in the v13 documents")
	}
}

func TestV13ContractDocStatesTheSurfacePassAndGating(t *testing.T) {
	contract := sectionBetween(t, readV13Doc(t, v13ContractDoc, false), v13ContractSectionHead, "")
	for _, want := range []string{
		"bench_version >= 13",
		"Salt 0 is the public rehearsal default",
		"byte-identical to the unsalted path",
		"Owner decision — default taken",
		"N13",
		"N14",
		"≤12% target / 15% hard",
		"publicWireBenchVersion = 9",
	} {
		// Markdown wraps prose, so whitespace in the wanted phrase matches any run
		// of whitespace in the document.
		pattern := regexp.MustCompile(strings.ReplaceAll(regexp.QuoteMeta(want), " ", `\s+`))
		if !pattern.MatchString(contract) {
			t.Errorf("bench-versions.md v13 section does not state %q", want)
		}
	}
	for _, path := range []string{v13ContractDoc, v13ProjectionDoc} {
		if v13ConflictMarker.MatchString(readV13Doc(t, path, false)) {
			t.Errorf("%s carries a merge conflict marker", path)
		}
	}
}
