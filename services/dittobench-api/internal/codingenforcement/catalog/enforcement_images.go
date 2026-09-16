package catalog

import (
	"errors"
	"maps"
	"regexp"
	"slices"
	"strings"
)

// EnforcementImagesSchema names the pinned per-language probe image set.
//
// Peyton (2026-09-15): "every language image" is every image the approval
// names, run with the approved grading profile's limits and timeouts but with
// each language's own explicitly recorded build and test commands. No argv is
// shared across languages unless the document records it for each one.
const EnforcementImagesSchema = "dittobench-coding-native-enforcement-images-v1"

// TestGroups is the hosted grading profile's fixed test group order.
var TestGroups = []string{"hidden", "visible"}

var (
	argvText        = regexp.MustCompile(`^[\x21-\x7e]{1,256}$`)
	argvExecutable  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	ociImageDigest  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	generalShells   = []string{"bash", "cmd", "dash", "env", "fish", "powershell", "pwsh", "sh", "zsh"}
	errEnforcement  = errors.New("enforcement images are malformed")
	enforcementKeys = []string{"grading_profile_sha256", "images", "schema"}
	imageEntryKeys  = []string{"build_argv", "image_digest", "test_argv"}
)

// TrustedTestDriver is the only executable a hosted test command may name.
const TrustedTestDriver = "dittobench-test-driver"

// EnforcementImage is one language's pinned probe image and commands.
type EnforcementImage struct {
	ImageDigest string
	BuildArgv   []string
	TestArgv    map[string][]string
}

// EnforcementImages is the decoded, validated image set.
type EnforcementImages struct {
	SHA256               string
	GradingProfileSHA256 string
	Images               map[string]EnforcementImage
}

// ParseEnforcementImages requires canonical bytes with closed keys, every
// catalog language exactly once, a distinct OCI manifest digest per language,
// and bounded commands: a bare non-shell executable, and the trusted test
// driver for both test groups.
func ParseEnforcementImages(raw []byte) (EnforcementImages, error) {
	decoded, err := ParseCanonical(raw)
	if err != nil {
		return EnforcementImages{}, errEnforcement
	}
	object, err := exactObject(decoded, enforcementKeys)
	if err != nil || object["schema"] != EnforcementImagesSchema {
		return EnforcementImages{}, errEnforcement
	}
	profile, ok := object["grading_profile_sha256"].(string)
	if !ok || !sha256Hex.MatchString(profile) || profile == zeroSHA256 {
		return EnforcementImages{}, errEnforcement
	}
	images, ok := object["images"].(map[string]any)
	if !ok || !slices.Equal(slices.Sorted(maps.Keys(images)), Languages) {
		return EnforcementImages{}, errEnforcement
	}
	result := EnforcementImages{SHA256: digestOf(raw), GradingProfileSHA256: profile, Images: map[string]EnforcementImage{}}
	seen := map[string]bool{}
	for _, language := range Languages {
		entry, err := exactObject(images[language], imageEntryKeys)
		if err != nil {
			return EnforcementImages{}, errEnforcement
		}
		image, ok := entry["image_digest"].(string)
		if !ok || !ociImageDigest.MatchString(image) || image == "sha256:"+zeroSHA256 || seen[image] {
			return EnforcementImages{}, errEnforcement
		}
		seen[image] = true
		build, ok := argv(entry["build_argv"], false)
		if !ok {
			return EnforcementImages{}, errEnforcement
		}
		tests, err := exactObject(entry["test_argv"], TestGroups)
		if err != nil {
			return EnforcementImages{}, errEnforcement
		}
		parsed := EnforcementImage{ImageDigest: image, BuildArgv: build, TestArgv: map[string][]string{}}
		for _, group := range TestGroups {
			if parsed.TestArgv[group], ok = argv(tests[group], true); !ok {
				return EnforcementImages{}, errEnforcement
			}
			if language == "rust" && !RustTestArgv(parsed.TestArgv[group], group) {
				return EnforcementImages{}, errEnforcement
			}
		}
		result.Images[language] = parsed
	}
	return result, nil
}

func argv(value any, test bool) ([]string, bool) {
	list, ok := value.([]any)
	if !ok || len(list) == 0 || len(list) > 64 {
		return nil, false
	}
	result := make([]string, len(list))
	total := 0
	for index, item := range list {
		text, ok := item.(string)
		if !ok || !argvText.MatchString(text) {
			return nil, false
		}
		result[index], total = text, total+len(text)
	}
	executable := result[0]
	if total > 8192 || !argvExecutable.MatchString(executable) || slices.Contains(generalShells, strings.ToLower(executable)) ||
		(test && executable != TrustedTestDriver) {
		return nil, false
	}
	return result, true
}

var rustAuthorityPart = regexp.MustCompile(`^[A-Za-z0-9_-][A-Za-z0-9._-]*$`)

// RustTestArgv reports whether argv is the production Rust driver's authority
// command for group: `dittobench-test-driver` with exactly --group,
// --authority (a bounded relative .json path) and --authority-sha256. It
// mirrors codingexecutor's rustCommand, which refuses any other Rust test
// command, and additionally requires --group to name the slot it is pinned in.
func RustTestArgv(argv []string, group string) bool {
	if len(argv) != 7 || argv[0] != TrustedTestDriver {
		return false
	}
	fields := map[string]string{}
	for index := 1; index < len(argv); index += 2 {
		if _, repeated := fields[argv[index]]; repeated {
			return false
		}
		fields[argv[index]] = argv[index+1]
	}
	authority, digest := fields["--authority"], fields["--authority-sha256"]
	parts := strings.Split(authority, "/")
	if len(fields) != 3 || fields["--group"] != group || authority == "" || len(authority) > 240 ||
		!strings.HasSuffix(authority, ".json") || len(parts) > 8 || !sha256Hex.MatchString(digest) {
		return false
	}
	for _, part := range parts {
		if !rustAuthorityPart.MatchString(part) {
			return false
		}
	}
	return true
}
