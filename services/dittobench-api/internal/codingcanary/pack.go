package codingcanary

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"io/fs"
	"math"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
)

const (
	publicCanarySchema          = "dittobench-coding-public-certification-canary-v1"
	publicCanaryTaskID          = "PRACTICE-LEDGER-001"
	publicCanaryProfileID       = "public-certification-v1"
	maximumCanonicalBytes       = 1 << 20
	lockedInferencePolicySHA256 = "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"
	// publicCanaryVisibleWorkspaceSHA256 binds the visible workspace, which the
	// manifest does not list. It is the SHA-256 of the `sha256sum` lines
	// ("<sha256>  <path>\n") for every workspace file, sorted by path with
	// ignoredPackEntry applied. Reproduce it from the workspace directory with:
	//
	//	find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name .DS_Store |
	//	  sed 's|^\./||' | LC_ALL=C sort | while read -r f; do sha256sum "$f"; done | sha256sum
	publicCanaryVisibleWorkspaceSHA256 = "507ba560291def373968ed9d59f959d1fe814e816de5e8f650d8e7aa45cc8b1b"
	maximumPackFileBytes               = 4 << 20
	maximumPackTreeBytes               = 16 << 20
	maximumPackTreeFiles               = 256
)

// packFile is one regular file of a verified capsule tree.
type packFile struct {
	path   string
	sha256 string
	size   int64
	body   []byte
}

type PublicPack struct {
	Root                  string
	CanaryManifestSHA256  string
	RunnerPlanSHA256      string
	GraderPlanSHA256      string
	ResourceProfileSHA256 string
	InferencePolicySHA256 string
	TaskID                string
	VisibleDir            string
	GraderDir             string
	LockedPolicyPath      string
	IssueTitle            string
	IssueDescription      string
	EditablePaths         []string
	TestCommandIDs        []string
	BuildCommandIDs       []string
	CPUQuotaMillis        uint32
	MemoryLimitBytes      uint64
	PidsLimit             uint32
	// graderFiles is the manifest's grader_plan.grader_files, the only files
	// the grader tree may contain.
	graderFiles []packFile
}

func LoadPublicPack(repoRoot string) (PublicPack, error) {
	var zero PublicPack
	if repoRoot == "" || !filepath.IsAbs(repoRoot) {
		return zero, ErrInvalid
	}
	manifestPath := filepath.Join(repoRoot, "research", "dittobench-coding-datagen", "certification", "v1", "manifest.json")
	body, err := os.ReadFile(manifestPath)
	if err != nil || len(body) == 0 || len(body) > maximumCanonicalBytes {
		return zero, ErrInvalid
	}
	var manifest map[string]any
	if err := json.Unmarshal(body, &manifest); err != nil {
		return zero, ErrInvalid
	}
	canonical, err := canonicalJSON(manifest)
	if err != nil || string(body) != string(canonical) {
		return zero, ErrInvalid
	}
	if schemaOf(manifest) != publicCanarySchema || intOf(manifest["coding_contract_version"]) != 1 ||
		boolOf(manifest["weight_eligible"]) || stringOf(manifest["corpus_scope"]) != "public_certification" {
		return zero, ErrInvalid
	}
	runner, _ := manifest["runner_plan"].(map[string]any)
	grader, _ := manifest["grader_plan"].(map[string]any)
	resource, _ := manifest["resource_profile"].(map[string]any)
	inference, _ := manifest["inference_policy"].(map[string]any)
	if stringOf(runner["task_id"]) != publicCanaryTaskID || stringOf(grader["task_id"]) != publicCanaryTaskID ||
		stringOf(resource["network"]) != "none" {
		return zero, ErrInvalid
	}
	cpus := intOf(resource["cpus_milli"])
	memoryMiB := intOf(resource["memory_mib"])
	pids := intOf(resource["pids"])
	if cpus < 1 || cpus > 256_000 || memoryMiB < 1 || memoryMiB > 1_048_576 || pids < 1 || pids > 1_000_000 {
		return zero, ErrInvalid
	}
	policyPath := filepath.Join(repoRoot, filepath.FromSlash(stringOf(inference["path"])))
	policyBody, err := os.ReadFile(policyPath)
	if err != nil {
		return zero, ErrInvalid
	}
	policyDigest := sha256.Sum256(policyBody)
	if hex.EncodeToString(policyDigest[:]) != lockedInferencePolicySHA256 ||
		stringOf(inference["sha256"]) != lockedInferencePolicySHA256 {
		return zero, ErrInvalid
	}
	runnerSHA, err := digestCanonicalObject(runner)
	if err != nil {
		return zero, err
	}
	graderSHA, err := digestCanonicalObject(grader)
	if err != nil {
		return zero, err
	}
	resourceSHA, err := digestCanonicalObject(resource)
	if err != nil {
		return zero, err
	}
	manifestSHA := sha256.Sum256(body)
	visible := filepath.Join(repoRoot, "research", "dittobench-coding-datagen", "certification", "v1", "capsules", publicCanaryTaskID, "visible", "workspace")
	graderDir := filepath.Join(repoRoot, "research", "dittobench-coding-datagen", "certification", "v1", "capsules", publicCanaryTaskID, "grader")
	graderFiles, err := manifestGraderFiles(grader["grader_files"])
	if err != nil {
		return zero, err
	}
	pack := PublicPack{
		Root: repoRoot, CanaryManifestSHA256: hex.EncodeToString(manifestSHA[:]),
		RunnerPlanSHA256: runnerSHA, GraderPlanSHA256: graderSHA,
		ResourceProfileSHA256: resourceSHA, InferencePolicySHA256: lockedInferencePolicySHA256,
		TaskID: publicCanaryTaskID, VisibleDir: visible, GraderDir: graderDir, LockedPolicyPath: policyPath,
		IssueTitle: "Preserve reference identity", IssueDescription: "Reference normalization removes surrounding whitespace but currently changes valid reference identity. Preserve the reference while normalizing it.",
		EditablePaths: stringSlice(runner["editable_paths"]), TestCommandIDs: stringSlice(runner["test_command_ids"]),
		BuildCommandIDs: stringSlice(runner["build_command_ids"]),
		CPUQuotaMillis:  uint32(cpus), MemoryLimitBytes: uint64(memoryMiB) * 1024 * 1024, PidsLimit: uint32(pids),
		graderFiles: graderFiles,
	}
	if err := pack.Verify(); err != nil {
		return zero, err
	}
	return pack, nil
}

// Verify re-reads both capsule trees. The grader tree must contain exactly the
// manifest's grader_files (path, size, and SHA-256), and the visible workspace
// must match its pinned listing digest. Any other file, link, or special file
// fails closed. Only ignoredPackEntry names (interpreter caches and Finder
// metadata, never bundled) are skipped.
func (pack PublicPack) Verify() error {
	_, _, err := pack.verifiedTrees()
	return err
}

func (pack PublicPack) verifiedTrees() (visible []packFile, grader []packFile, err error) {
	if pack.VisibleDir == "" || pack.GraderDir == "" || len(pack.graderFiles) == 0 {
		return nil, nil, ErrInvalid
	}
	visible, err = readPackTree(pack.VisibleDir)
	if err != nil || packListingSHA256(visible) != publicCanaryVisibleWorkspaceSHA256 {
		return nil, nil, ErrInvalid
	}
	grader, err = readPackTree(pack.GraderDir)
	if err != nil || !sameFileSet(grader, pack.graderFiles) {
		return nil, nil, ErrInvalid
	}
	return visible, grader, nil
}

// ignoredPackEntry is the one exclusion rule shared by the loader, the root
// .dockerignore (**/__pycache__, **/*.pyc, **/.DS_Store), and the scorer image
// tests. Ignored entries are neither verified nor bundled.
func ignoredPackEntry(name string, directory bool) bool {
	if directory {
		return name == "__pycache__"
	}
	return name == ".DS_Store" || strings.HasSuffix(name, ".pyc")
}

// readPackTree returns every regular file under root in lexical walk order.
func readPackTree(root string) ([]packFile, error) {
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() {
		return nil, ErrInvalid
	}
	var files []packFile
	var total int64
	err = filepath.WalkDir(root, func(current string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return ErrInvalid
		}
		if current == root {
			return nil
		}
		if entry.IsDir() {
			if ignoredPackEntry(entry.Name(), true) {
				return filepath.SkipDir
			}
			return nil
		}
		if !entry.Type().IsRegular() {
			return ErrInvalid
		}
		if ignoredPackEntry(entry.Name(), false) {
			return nil
		}
		relative, err := filepath.Rel(root, current)
		if err != nil {
			return ErrInvalid
		}
		body, err := readBoundedPackFile(current)
		if err != nil {
			return err
		}
		total += int64(len(body))
		if len(files) >= maximumPackTreeFiles || total > maximumPackTreeBytes {
			return ErrInvalid
		}
		digest := sha256.Sum256(body)
		files = append(files, packFile{
			path: filepath.ToSlash(relative), sha256: hex.EncodeToString(digest[:]),
			size: int64(len(body)), body: body,
		})
		return nil
	})
	if err != nil || len(files) == 0 {
		return nil, ErrInvalid
	}
	return files, nil
}

func readBoundedPackFile(name string) ([]byte, error) {
	handle, err := os.Open(name)
	if err != nil {
		return nil, ErrInvalid
	}
	defer handle.Close()
	info, err := handle.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return nil, ErrInvalid
	}
	body, err := io.ReadAll(io.LimitReader(handle, maximumPackFileBytes+1))
	if err != nil || len(body) > maximumPackFileBytes {
		return nil, ErrInvalid
	}
	return body, nil
}

func packListingSHA256(files []packFile) string {
	sorted := append([]packFile(nil), files...)
	sort.Slice(sorted, func(left, right int) bool { return sorted[left].path < sorted[right].path })
	digest := sha256.New()
	for _, file := range sorted {
		_, _ = io.WriteString(digest, file.sha256+"  "+file.path+"\n")
	}
	return hex.EncodeToString(digest.Sum(nil))
}

func sameFileSet(actual []packFile, expected []packFile) bool {
	if len(actual) != len(expected) {
		return false
	}
	byPath := make(map[string]packFile, len(expected))
	for _, file := range expected {
		byPath[file.path] = file
	}
	for _, file := range actual {
		want, ok := byPath[file.path]
		if !ok || want.sha256 != file.sha256 || want.size != file.size {
			return false
		}
		delete(byPath, file.path)
	}
	return len(byPath) == 0
}

func manifestGraderFiles(value any) ([]packFile, error) {
	items, ok := value.([]any)
	if !ok || len(items) == 0 || len(items) > maximumPackTreeFiles {
		return nil, ErrInvalid
	}
	files := make([]packFile, 0, len(items))
	seen := make(map[string]struct{}, len(items))
	for _, item := range items {
		entry, ok := item.(map[string]any)
		if !ok || len(entry) != 3 {
			return nil, ErrInvalid
		}
		name, _ := entry["path"].(string)
		digest, _ := entry["sha256"].(string)
		size, sizeOK := entry["size_bytes"].(float64)
		if !validPackPath(name) || !validSHA256(digest) || !sizeOK || size < 0 ||
			size > maximumPackFileBytes || size != math.Trunc(size) {
			return nil, ErrInvalid
		}
		if _, duplicate := seen[name]; duplicate {
			return nil, ErrInvalid
		}
		seen[name] = struct{}{}
		files = append(files, packFile{path: name, sha256: digest, size: int64(size)})
	}
	return files, nil
}

func validPackPath(value string) bool {
	if value == "" || len(value) > 256 || strings.HasPrefix(value, "/") || path.Clean(value) != value ||
		strings.ContainsAny(value, "\\\x00") {
		return false
	}
	components := strings.Split(value, "/")
	for index, component := range components {
		if component == "" || component == "." || component == ".." ||
			ignoredPackEntry(component, index < len(components)-1) {
			return false
		}
	}
	return true
}

func (pack PublicPack) Matches(request Request) bool {
	return pack.CanaryManifestSHA256 == request.CanaryManifestSHA256 &&
		pack.RunnerPlanSHA256 == request.RunnerPlanSHA256 &&
		pack.GraderPlanSHA256 == request.GraderPlanSHA256 &&
		pack.ResourceProfileSHA256 == request.ResourceProfileSHA256 &&
		pack.InferencePolicySHA256 == request.InferencePolicySHA256
}

func digestCanonicalObject(value map[string]any) (string, error) {
	body, err := canonicalJSON(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:]), nil
}

func canonicalJSON(value any) ([]byte, error) {
	normalized, err := normalizeJSON(value)
	if err != nil {
		return nil, err
	}
	body, err := json.Marshal(normalized)
	if err != nil {
		return nil, err
	}
	return append(body, '\n'), nil
}

func normalizeJSON(value any) (any, error) {
	switch typed := value.(type) {
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		normalized := make(map[string]any, len(keys))
		for _, key := range keys {
			child, err := normalizeJSON(typed[key])
			if err != nil {
				return nil, err
			}
			normalized[key] = child
		}
		return normalized, nil
	case []any:
		normalized := make([]any, len(typed))
		for index, item := range typed {
			child, err := normalizeJSON(item)
			if err != nil {
				return nil, err
			}
			normalized[index] = child
		}
		return normalized, nil
	default:
		return typed, nil
	}
}

func schemaOf(value map[string]any) string { return stringOf(value["schema"]) }

func stringOf(value any) string {
	text, _ := value.(string)
	return text
}

func intOf(value any) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	default:
		return 0
	}
}

func boolOf(value any) bool {
	flag, _ := value.(bool)
	return flag
}

func stringSlice(value any) []string {
	items, _ := value.([]any)
	result := make([]string, 0, len(items))
	for _, item := range items {
		text, _ := item.(string)
		if text != "" {
			result = append(result, text)
		}
	}
	return result
}
