package catalog

import (
	"errors"
	"maps"
	"regexp"
	"slices"
	"strings"
)

// PreexecFixturesSchema names the public pre-exec fixture manifest. The
// collector runs these subjects, never a private task, through each language's
// own recorded test command; the approval pins this document as
// preexec_fixtures_sha256.
const PreexecFixturesSchema = "dittobench-coding-native-preexec-fixtures-v1"

// PreexecControls is the fixed control order of the pre-exec catalog phase.
var PreexecControls = []string{"hang", "pass", "wrong"}

// fixtureRoot bounds every recorded path to the reviewed fixture tree, so a
// manifest can never name another file of the checkout.
const fixtureRoot = "services/dittobench-api/internal/codingenforcement/fixtures/preexec/"

var (
	errPreexecFixtures = errors.New("preexec fixtures are malformed")
	preexecFixtureKeys = []string{"languages", "schema"}
	fixtureRefKeys     = []string{"path", "sha256"}
	hostileEntryKeys   = []string{"outcome", "subject"}
	controlEntryKeys   = []string{"subject"}
	fixturePath        = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$`)
	outcomeToken       = regexp.MustCompile(`^[a-z][a-z_]{0,31}$`)
)

// PreexecFixtureRef is one recorded fixture file: its reviewed path and bytes.
type PreexecFixtureRef struct {
	Path   string
	SHA256 string
}

// PreexecHostile is one hostile subject and the outcome the catalog expects
// from it. The outcome is recorded, never inferred at collection time.
type PreexecHostile struct {
	Outcome string
	Subject PreexecFixtureRef
}

// PreexecLanguage is one language's fixture set: the recorded test command and
// expected total the driver must report, the shared hidden suite, the three
// controls and every hostile subject.
type PreexecLanguage struct {
	TestGroup           string
	TestArgv            []string
	ExpectedTotal       uint32
	ControlsSuite       PreexecFixtureRef
	ControlsSuiteSHA256 string
	Controls            map[string]PreexecFixtureRef
	Hostile             map[string]PreexecHostile
	// Module is Go's go.mod; Authority is Rust's pinned hidden authority. Each
	// is present only for its own language.
	Module    *PreexecFixtureRef
	Authority *PreexecFixtureRef
}

// PreexecFixtures is the decoded, validated fixture manifest.
type PreexecFixtures struct {
	SHA256    string
	Languages map[string]PreexecLanguage
}

// Files lists every recorded fixture file once, sorted by path, so a caller can
// verify the reviewed checkout before staging anything.
func (f PreexecFixtures) Files() []PreexecFixtureRef {
	seen := map[string]string{}
	for _, language := range Languages {
		entry := f.Languages[language]
		refs := []PreexecFixtureRef{entry.ControlsSuite}
		for _, name := range PreexecControls {
			refs = append(refs, entry.Controls[name])
		}
		for _, name := range slices.Sorted(maps.Keys(entry.Hostile)) {
			refs = append(refs, entry.Hostile[name].Subject)
		}
		if entry.Module != nil {
			refs = append(refs, *entry.Module)
		}
		if entry.Authority != nil {
			refs = append(refs, *entry.Authority)
		}
		for _, ref := range refs {
			seen[ref.Path] = ref.SHA256
		}
	}
	result := make([]PreexecFixtureRef, 0, len(seen))
	for _, path := range slices.Sorted(maps.Keys(seen)) {
		result = append(result, PreexecFixtureRef{Path: path, SHA256: seen[path]})
	}
	return result
}

// ParsePreexecFixtures requires canonical bytes with closed keys, every catalog
// language exactly once, a recorded path inside the reviewed fixture tree for
// every file, the trusted test driver as the only test executable, and an
// expected total of at least two: a suite of fewer than two tests cannot tell a
// wrong control from a crashed grader.
func ParsePreexecFixtures(raw []byte) (PreexecFixtures, error) {
	decoded, err := ParseCanonical(raw)
	if err != nil {
		return PreexecFixtures{}, errPreexecFixtures
	}
	object, err := exactObject(decoded, preexecFixtureKeys)
	if err != nil || object["schema"] != PreexecFixturesSchema {
		return PreexecFixtures{}, errPreexecFixtures
	}
	languages, ok := object["languages"].(map[string]any)
	if !ok || !slices.Equal(slices.Sorted(maps.Keys(languages)), Languages) {
		return PreexecFixtures{}, errPreexecFixtures
	}
	result := PreexecFixtures{SHA256: digestOf(raw), Languages: map[string]PreexecLanguage{}}
	for _, language := range Languages {
		entry, err := parsePreexecLanguage(languages[language], language)
		if err != nil {
			return PreexecFixtures{}, err
		}
		result.Languages[language] = entry
	}
	return result, nil
}

func parsePreexecLanguage(value any, language string) (PreexecLanguage, error) {
	keys := []string{"controls", "controls_suite", "controls_suite_sha256", "expected_total", "hostile", "test_argv", "test_group"}
	switch language {
	case "go":
		keys = append(keys, "module")
	case "rust":
		keys = append(keys, "authority")
	}
	slices.Sort(keys)
	object, err := exactObject(value, keys)
	if err != nil {
		return PreexecLanguage{}, errPreexecFixtures
	}
	group, ok := object["test_group"].(string)
	if !ok || !slices.Contains(TestGroups, group) {
		return PreexecLanguage{}, errPreexecFixtures
	}
	testArgv, ok := argv(object["test_argv"], true)
	if !ok {
		return PreexecLanguage{}, errPreexecFixtures
	}
	if language == "rust" && !RustTestArgv(testArgv, group) {
		return PreexecLanguage{}, errPreexecFixtures
	}
	total, ok := nonNegative(object["expected_total"])
	if !ok || total < 2 || total > 1024 {
		return PreexecLanguage{}, errPreexecFixtures
	}
	suite, err := parseFixtureRef(object["controls_suite"], language)
	if err != nil {
		return PreexecLanguage{}, err
	}
	suiteDigest, ok := object["controls_suite_sha256"].(string)
	if !ok || suiteDigest != suite.SHA256 {
		return PreexecLanguage{}, errPreexecFixtures
	}
	controls, err := exactObject(object["controls"], PreexecControls)
	if err != nil {
		return PreexecLanguage{}, errPreexecFixtures
	}
	parsed := PreexecLanguage{
		TestGroup: group, TestArgv: testArgv, ExpectedTotal: uint32(total),
		ControlsSuite: suite, ControlsSuiteSHA256: suiteDigest,
		Controls: map[string]PreexecFixtureRef{}, Hostile: map[string]PreexecHostile{},
	}
	for _, name := range PreexecControls {
		control, err := exactObject(controls[name], controlEntryKeys)
		if err != nil {
			return PreexecLanguage{}, errPreexecFixtures
		}
		if parsed.Controls[name], err = parseFixtureRef(control["subject"], language); err != nil {
			return PreexecLanguage{}, err
		}
	}
	hostile, ok := object["hostile"].(map[string]any)
	if !ok || len(hostile) == 0 || len(hostile) > 64 {
		return PreexecLanguage{}, errPreexecFixtures
	}
	for _, name := range slices.Sorted(maps.Keys(hostile)) {
		if !fixturePath.MatchString(name) || strings.Contains(name, "/") {
			return PreexecLanguage{}, errPreexecFixtures
		}
		item, err := exactObject(hostile[name], hostileEntryKeys)
		if err != nil {
			return PreexecLanguage{}, errPreexecFixtures
		}
		// The outcome vocabulary belongs to the catalog: the caller binds each
		// recorded outcome to the loaded catalog's set, and the offline
		// verifier refuses an unknown one. Here it is only a bounded token.
		outcome, ok := item["outcome"].(string)
		if !ok || !outcomeToken.MatchString(outcome) {
			return PreexecLanguage{}, errPreexecFixtures
		}
		subject, err := parseFixtureRef(item["subject"], language)
		if err != nil {
			return PreexecLanguage{}, err
		}
		parsed.Hostile[name] = PreexecHostile{Outcome: outcome, Subject: subject}
	}
	for key, target := range map[string]**PreexecFixtureRef{"module": &parsed.Module, "authority": &parsed.Authority} {
		raw, present := object[key]
		if !present {
			continue
		}
		ref, err := parseFixtureRef(raw, language)
		if err != nil {
			return PreexecLanguage{}, err
		}
		*target = &ref
	}
	return parsed, nil
}

func parseFixtureRef(value any, language string) (PreexecFixtureRef, error) {
	object, err := exactObject(value, fixtureRefKeys)
	if err != nil {
		return PreexecFixtureRef{}, errPreexecFixtures
	}
	path, ok := object["path"].(string)
	if !ok || !fixturePath.MatchString(path) || strings.Contains(path, "..") ||
		!strings.HasPrefix(path, fixtureRoot+language+"/") {
		return PreexecFixtureRef{}, errPreexecFixtures
	}
	digest, ok := object["sha256"].(string)
	if !ok || !sha256Hex.MatchString(digest) || digest == zeroSHA256 {
		return PreexecFixtureRef{}, errPreexecFixtures
	}
	return PreexecFixtureRef{Path: path, SHA256: digest}, nil
}
