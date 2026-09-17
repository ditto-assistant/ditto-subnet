package gen

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// The Bench v13 contract is published in four documents. This test keeps them
// honest against each other and against the tree: every public rule a v13
// harness can be scored on has a statement in the datagen contract AND in the
// wire protocol, each statement points at a vector test that exists in the
// tree, every vector test the docs name exists in the tree, and each document
// carries exactly one v13 section. A renamed test, a rule documented without a
// real vector, a conflict marker, or a second v13 umbrella re-introduced by a
// stack merge fails here instead of at review.
//
// The v13 PRs land as independent branches; each documented its lever under
// an interim per-PR heading in the same files. The consolidated documents fold
// those headings into one section per document, and the heading test below is
// what keeps a later merge from resurrecting one of them beside the
// consolidated text.
//
// While the sibling PRs are still unmerged, the vector names they carry do not
// exist on this tree. Setting DITTOBENCH_V13_DOCS_ALLOW_PENDING=1 lets a name
// that carries its issue marker (`(#NNNN)` on the same or the following line)
// stand in for the test; CI never sets it, so the published contract cannot
// merge with a vector that does not exist.

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
	v13StarterProtocolHead = "## Bench v13 additions (harness-visible)"
	v13StarterReadmeHead   = "## Bench v13: how to stay inside the gates"
	v13PendingEnv          = "DITTOBENCH_V13_DOCS_ALLOW_PENDING"
)

// v13PublicRules is every scorer- or grader-visible v13 rule the issue #1853
// acceptance list names. Each token must appear (case-insensitively) in both
// contract documents, inside a heading block that also names a vector test
// declared in the tree.
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

// v13WireSectionsContested are the wire-protocol sections more than one v13 PR
// authored. Each must appear exactly once after consolidation.
var v13WireSectionsContested = []string{
	"### bench_version 13: catalog capture and the catalog-present gate",
	"### bench_version 13: staged seeding waves and the `/seed` ingest acknowledgement",
	"### bench_version 13: claim-span provenance and causal model dependence",
	"### bench_version 13: twin / pair post-pass",
}

var (
	v13TestRefPattern    = regexp.MustCompile(`\bTest[A-Z][A-Za-z0-9_]*`)
	v13IssueMarker       = regexp.MustCompile(`#\d{4}\b`)
	v13ConflictMarker    = regexp.MustCompile(`(?m)^(<<<<<<<|=======|>>>>>>>)`)
	v13UmbrellaHeading   = regexp.MustCompile(`(?m)^## Bench v13\b.*$`)
	v13VersionTableRow   = regexp.MustCompile(`(?m)^\| 13 .*$`)
	v13WireSubsection    = regexp.MustCompile("(?m)^### bench_version 13: .*$")
	v13StarterSubsection = regexp.MustCompile(`(?m)^### Bench v13:.*$`)
)

func v13PendingAllowed() bool {
	return os.Getenv(v13PendingEnv) == "1"
}

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

// v13KnownTests is the set of Test* functions declared in the tree, with a
// sentinel so a broken walk cannot pass as "no references".
func v13KnownTests(t *testing.T) map[string]bool {
	t.Helper()
	known := goTestFuncsInTree(t, "..", "../../../services", "../../../workers")
	if !known["TestV12KnownVector"] {
		t.Fatalf("tree walk did not find TestV12KnownVector; the reference scan is not looking at the tree")
	}
	return known
}

// v13BlockNamesAVector reports whether a heading block names a vector test
// declared in the tree. Under the explicit pending opt-in a Test* token that
// carries its issue marker in the same block also counts.
func v13BlockNamesAVector(block string, known map[string]bool) bool {
	for _, ref := range v13TestRefPattern.FindAllString(block, -1) {
		if known[ref] {
			return true
		}
		if v13PendingAllowed() && v13IssueMarker.MatchString(block) {
			return true
		}
	}
	return false
}

func TestV13ContractDocCoversEveryPublicRuleWithAVector(t *testing.T) {
	contract := sectionBetween(t, readV13Doc(t, v13ContractDoc, false), v13ContractSectionHead, "")
	wire := sectionBetween(t, readV13Doc(t, v13WireProtocolDoc, true), v13WireSectionHead, v13WireSectionEnd)
	known := v13KnownTests(t)

	for name, section := range map[string]string{"bench-versions.md": contract, "PROTOCOL.md": wire} {
		blocks := headingBlocks(section)
		for _, rule := range v13PublicRules {
			stated, vectored := false, false
			for _, block := range blocks {
				if !strings.Contains(strings.ToLower(block), strings.ToLower(rule)) {
					continue
				}
				stated = true
				if v13BlockNamesAVector(block, known) {
					vectored = true
					break
				}
			}
			if !stated {
				t.Errorf("%s: v13 rule %q has no documented statement", name, rule)
			} else if !vectored {
				t.Errorf("%s: v13 rule %q is stated but no block stating it names a vector test declared in the tree (set %s=1 only for a pending sibling PR)", name, rule, v13PendingEnv)
			}
		}
	}
}

func TestV13ContractDocVectorReferencesExistInTree(t *testing.T) {
	docs := map[string]string{
		v13ContractDoc:        sectionBetween(t, readV13Doc(t, v13ContractDoc, false), v13ContractSectionHead, ""),
		v13ProjectionDoc:      readV13Doc(t, v13ProjectionDoc, false),
		v13WireProtocolDoc:    sectionBetween(t, readV13Doc(t, v13WireProtocolDoc, true), v13WireSectionHead, v13WireSectionEnd),
		v13StarterProtocolDoc: readV13Doc(t, v13StarterProtocolDoc, true),
		v13StarterReadmeDoc:   readV13Doc(t, v13StarterReadmeDoc, true),
	}
	known := v13KnownTests(t)
	seen := 0
	missing := map[string]bool{}
	for path, body := range docs {
		lines := strings.Split(body, "\n")
		for lineNo, line := range lines {
			for _, ref := range v13TestRefPattern.FindAllString(line, -1) {
				seen++
				if known[ref] {
					continue
				}
				if v13PendingAllowed() {
					window := line
					if lineNo+1 < len(lines) {
						window += "\n" + lines[lineNo+1]
					}
					if v13IssueMarker.MatchString(window) {
						continue
					}
				}
				missing[ref] = true
				t.Errorf("%s:%d names %s, which is not declared in the tree", path, lineNo+1, ref)
			}
		}
	}
	if seen == 0 {
		t.Fatalf("no vector test references found in the v13 documents")
	}
	if len(missing) > 0 {
		names := make([]string, 0, len(missing))
		for name := range missing {
			names = append(names, name)
		}
		sort.Strings(names)
		t.Logf("%d documented vector tests are not declared in this tree; if their PRs are still unmerged, run with %s=1 to check the prose alone: %s", len(names), v13PendingEnv, strings.Join(names, ", "))
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
	for path, monorepoOnly := range map[string]bool{
		v13ContractDoc:        false,
		v13ProjectionDoc:      false,
		v13WireProtocolDoc:    true,
		v13StarterProtocolDoc: true,
		v13StarterReadmeDoc:   true,
	} {
		body, err := os.ReadFile(path)
		if err != nil {
			if monorepoOnly && os.IsNotExist(err) {
				continue
			}
			t.Fatalf("read %s: %v", path, err)
		}
		if v13ConflictMarker.Match(body) {
			t.Errorf("%s carries a merge conflict marker", path)
		}
	}
}

// countLines reports how many lines of doc match pattern and returns them.
func countLines(pattern *regexp.Regexp, doc string) []string {
	return pattern.FindAllString(doc, -1)
}

func TestV13ContractDocHasOneSectionPerDocument(t *testing.T) {
	contract := readV13Doc(t, v13ContractDoc, false)
	if got := countLines(v13UmbrellaHeading, contract); len(got) != 1 || got[0] != v13ContractSectionHead {
		t.Errorf("bench-versions.md must carry exactly one `## Bench v13` heading, %q; found %d: %q (an interim per-PR umbrella was merged back in — fold it into the consolidated section)", v13ContractSectionHead, len(got), got)
	}
	if got := countLines(v13VersionTableRow, contract); len(got) != 1 {
		t.Errorf("bench-versions.md must carry exactly one `| 13 ` row in the version table; found %d: %q", len(got), got)
	}

	wireDoc := readV13Doc(t, v13WireProtocolDoc, true)
	if got := countLines(v13UmbrellaHeading, wireDoc); len(got) != 0 {
		t.Errorf("services PROTOCOL.md documents v13 under `### bench_version 13: …` subsections only; found top-level %q", got)
	}
	wireHeads := map[string]int{}
	for _, line := range countLines(v13WireSubsection, wireDoc) {
		wireHeads[line]++
	}
	for line, n := range wireHeads {
		if n != 1 {
			t.Errorf("services PROTOCOL.md carries %d copies of %q; a stack merge re-introduced a duplicate body", n, line)
		}
	}
	for _, want := range v13WireSectionsContested {
		if wireHeads[want] == 0 {
			t.Errorf("services PROTOCOL.md lost the consolidated section %q", want)
		}
	}
	if got := strings.Count(wireDoc, "\n"+v13WireSectionHead); got != 1 {
		t.Errorf("services PROTOCOL.md must carry exactly one %q heading; found %d", v13WireSectionHead, got)
	}

	starterProtocol := readV13Doc(t, v13StarterProtocolDoc, true)
	if got := strings.Count(starterProtocol, "\n"+v13StarterProtocolHead+"\n"); got != 1 {
		t.Errorf("starter-kit PROTOCOL.md must carry exactly one %q heading; found %d", v13StarterProtocolHead, got)
	}
	if got := countLines(v13StarterSubsection, starterProtocol); len(got) != 0 {
		t.Errorf("starter-kit PROTOCOL.md carries an interim `### Bench v13:` heading beside the consolidated section: %q", got)
	}

	starterReadme := readV13Doc(t, v13StarterReadmeDoc, true)
	if got := strings.Count(starterReadme, "\n"+v13StarterReadmeHead+"\n"); got != 1 {
		t.Errorf("starter-kit README.md must carry exactly one %q heading (docs/MINER.md links its anchor); found %d", v13StarterReadmeHead, got)
	}
	if got := countLines(v13StarterSubsection, starterReadme); len(got) != 0 {
		t.Errorf("starter-kit README.md carries an interim `### Bench v13:` heading beside the consolidated guide: %q", got)
	}
}
