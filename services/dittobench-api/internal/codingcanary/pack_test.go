package codingcanary

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestLoadPublicPackMatchesThePinnedLeaseIdentity(t *testing.T) {
	pack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	if pack.CanaryManifestSHA256 != "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903" {
		t.Fatalf("canary manifest sha=%s", pack.CanaryManifestSHA256)
	}
	if pack.InferencePolicySHA256 != lockedInferencePolicySHA256 || pack.TaskID != publicCanaryTaskID {
		t.Fatalf("pack identity=%+v", pack)
	}
	if pack.CPUQuotaMillis != 2000 || pack.MemoryLimitBytes != 1024*1024*1024 || pack.PidsLimit != 256 {
		t.Fatalf("resource envelope=%+v", pack)
	}
}

func TestPublicPackExecutionPlansValidate(t *testing.T) {
	pack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC)
	plans, err := pack.executionPlans(
		now, now.Add(20*time.Minute), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
		"sha256:"+repeatHex("2"),
	)
	if err != nil {
		t.Fatal(err)
	}
	if plans.runner.CaseID != publicCanaryTaskID || plans.grader.CaseID != publicCanaryTaskID {
		t.Fatalf("case ids runner=%s grader=%s", plans.runner.CaseID, plans.grader.CaseID)
	}
	if len(plans.visible) == 0 || len(plans.graderBundle) == 0 {
		t.Fatal("execution plans omitted capsule bytes")
	}
}

// The scorer image carries the certification/v1 tree and the locked policy,
// after the root .dockerignore drops interpreter caches and Finder metadata.
// Loading that image-shaped root must yield the same lease identity and the
// same execution bytes as the repository, whether or not such junk is present.
func TestLoadPublicPackFromAnImageShapedRootIgnoresOnlyCacheJunk(t *testing.T) {
	repoPack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	clean := copyImageShapedPack(t)
	junk := copyImageShapedPack(t)
	plantPackJunk(t, junk)
	for name, root := range map[string]string{"clean": clean, "junk": junk} {
		imagePack, err := LoadPublicPack(root)
		if err != nil {
			t.Fatalf("%s image-shaped certification root does not load: %v", name, err)
		}
		if imagePack.CanaryManifestSHA256 != "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903" ||
			imagePack.CanaryManifestSHA256 != repoPack.CanaryManifestSHA256 ||
			imagePack.RunnerPlanSHA256 != repoPack.RunnerPlanSHA256 ||
			imagePack.GraderPlanSHA256 != repoPack.GraderPlanSHA256 ||
			imagePack.ResourceProfileSHA256 != repoPack.ResourceProfileSHA256 ||
			imagePack.InferencePolicySHA256 != repoPack.InferencePolicySHA256 {
			t.Fatalf("%s image pack identity=%+v repo pack identity=%+v", name, imagePack, repoPack)
		}
		now := time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC)
		lease := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
		digest := "sha256:" + repeatHex("2")
		imagePlans, err := imagePack.executionPlans(now, now.Add(20*time.Minute), lease, digest)
		if err != nil {
			t.Fatal(err)
		}
		repoPlans, err := repoPack.executionPlans(now, now.Add(20*time.Minute), lease, digest)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(imagePlans.visible, repoPlans.visible) ||
			!bytes.Equal(imagePlans.graderBundle, repoPlans.graderBundle) ||
			!reflect.DeepEqual(imagePlans.runner, repoPlans.runner) ||
			!reflect.DeepEqual(imagePlans.grader, repoPlans.grader) {
			t.Fatalf("%s image-shaped certification root changes the execution plans", name)
		}
		if bytes.Contains(imagePlans.visible, []byte("__pycache__")) || bytes.Contains(imagePlans.graderBundle, []byte(".DS_Store")) {
			t.Fatalf("%s bundle carried ignored junk", name)
		}
	}
}

func TestLoadPublicPackRejectsTamperedExtraOrLinkedCapsuleFiles(t *testing.T) {
	capsule := filepath.Join("research", "dittobench-coding-datagen", "certification", "v1", "capsules", publicCanaryTaskID)
	visible := filepath.Join(capsule, "visible", "workspace")
	grader := filepath.Join(capsule, "grader")
	for name, mutate := range map[string]func(root string) error{
		"modified visible file": func(root string) error {
			return appendToFile(filepath.Join(root, visible, "app.py"), "\n# drift\n")
		},
		"extra visible file": func(root string) error {
			return os.WriteFile(filepath.Join(root, visible, "tests", "conftest.py"), []byte("x = 1\n"), 0o644)
		},
		"extra visible file beside junk": func(root string) error {
			return os.WriteFile(filepath.Join(root, visible, "tests", "__init__.py"), nil, 0o644)
		},
		"modified grader file with the same size": func(root string) error {
			target := filepath.Join(root, grader, "tests", "test_regression.py")
			body, err := os.ReadFile(target)
			if err != nil {
				return err
			}
			body[len(body)-2] ^= 0x01
			return os.WriteFile(target, body, 0o644)
		},
		"extra grader file": func(root string) error {
			return os.WriteFile(filepath.Join(root, grader, "tests", "test_extra.py"), []byte("pass\n"), 0o644)
		},
		"missing grader file": func(root string) error {
			return os.Remove(filepath.Join(root, grader, "tests", "test_regression.py"))
		},
		"linked visible file": func(root string) error {
			target := filepath.Join(root, visible, "app.py")
			if err := os.Rename(target, target+".real"); err != nil {
				return err
			}
			return os.Symlink("app.py.real", target)
		},
		"linked grader directory": func(root string) error {
			target := filepath.Join(root, grader)
			if err := os.Rename(target, target+"-real"); err != nil {
				return err
			}
			return os.Symlink(filepath.Base(target)+"-real", target)
		},
		"tampered locked policy": func(root string) error {
			return appendToFile(filepath.Join(root, "packages", "dittobench-coding-contract", "testdata", "coding_inference_policy_locked_v1.json"), " ")
		},
	} {
		t.Run(name, func(t *testing.T) {
			root := copyImageShapedPack(t)
			plantPackJunk(t, root)
			if err := mutate(root); err != nil {
				t.Fatal(err)
			}
			if _, err := LoadPublicPack(root); err == nil {
				t.Fatal("tampered certification root loaded")
			}
		})
	}
}

func TestPublicPackVerifyDetectsDriftAfterLoad(t *testing.T) {
	root := copyImageShapedPack(t)
	pack, err := LoadPublicPack(root)
	if err != nil {
		t.Fatal(err)
	}
	if err := pack.Verify(); err != nil {
		t.Fatal(err)
	}
	if err := appendToFile(filepath.Join(pack.GraderDir, "tests", "test_regression.py"), "\n"); err != nil {
		t.Fatal(err)
	}
	if err := pack.Verify(); err == nil {
		t.Fatal("drifted grader tree still verifies")
	}
	now := time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC)
	if _, err := pack.executionPlans(now, now.Add(20*time.Minute), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "sha256:"+repeatHex("2")); err == nil {
		t.Fatal("drifted grader tree still produces execution plans")
	}
}

func TestManifestGraderFilesAreStrict(t *testing.T) {
	valid := map[string]any{"path": "tests/test_regression.py", "sha256": repeatHex("a"), "size_bytes": float64(374)}
	if files, err := manifestGraderFiles([]any{valid}); err != nil || len(files) != 1 {
		t.Fatalf("files=%v err=%v", files, err)
	}
	with := func(key string, value any) map[string]any {
		entry := map[string]any{}
		for name, item := range valid {
			entry[name] = item
		}
		entry[key] = value
		return entry
	}
	for name, value := range map[string]any{
		"empty":             []any{},
		"not a list":        valid,
		"duplicate":         []any{valid, valid},
		"parent path":       []any{with("path", "../tests/test_regression.py")},
		"absolute path":     []any{with("path", "/tests/test_regression.py")},
		"unclean path":      []any{with("path", "tests//test_regression.py")},
		"ignored cache dir": []any{with("path", "tests/__pycache__/x.py")},
		"ignored file name": []any{with("path", "tests/x.pyc")},
		"uppercase digest":  []any{with("sha256", strings.ToUpper(repeatHex("a")))},
		"fractional size":   []any{with("size_bytes", 1.5)},
		"negative size":     []any{with("size_bytes", float64(-1))},
		"string size":       []any{with("size_bytes", "374")},
		"extra key":         []any{with("mode", "0644")},
	} {
		if _, err := manifestGraderFiles(value); err == nil {
			t.Errorf("%s grader_files were accepted", name)
		}
	}
}

// The loader's exclusion rule and the build context's exclusion rule must
// agree, or a stray cache file would either reach the image unverified or be
// verified in tests but not in the image.
func TestRootDockerignoreExcludesExactlyTheLoaderIgnoredNames(t *testing.T) {
	body, err := os.ReadFile(filepath.Join(repoRoot(t), ".dockerignore"))
	if err != nil {
		t.Fatal(err)
	}
	patterns := map[string]bool{}
	for _, line := range strings.Split(string(body), "\n") {
		patterns[strings.TrimSpace(line)] = true
	}
	for _, pattern := range []string{"**/__pycache__", "**/*.pyc", "**/.DS_Store"} {
		if !patterns[pattern] {
			t.Errorf(".dockerignore lacks %s", pattern)
		}
	}
	if !ignoredPackEntry("__pycache__", true) || !ignoredPackEntry("x.pyc", false) ||
		!ignoredPackEntry(".DS_Store", false) || ignoredPackEntry("__pycache__", false) ||
		ignoredPackEntry("x.py", false) || ignoredPackEntry("tests", true) {
		t.Fatal("loader exclusion rule changed without the build context")
	}
}

func copyImageShapedPack(t *testing.T) string {
	t.Helper()
	repo := repoRoot(t)
	root := t.TempDir()
	for _, relative := range []string{
		filepath.Join("research", "dittobench-coding-datagen", "certification", "v1"),
		filepath.Join("packages", "dittobench-coding-contract", "testdata", "coding_inference_policy_locked_v1.json"),
	} {
		source := filepath.Join(repo, relative)
		err := filepath.WalkDir(source, func(current string, entry os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if ignoredPackEntry(entry.Name(), entry.IsDir()) {
				if entry.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
			rel, err := filepath.Rel(repo, current)
			if err != nil {
				return err
			}
			target := filepath.Join(root, rel)
			if entry.IsDir() {
				return os.MkdirAll(target, 0o755)
			}
			if !entry.Type().IsRegular() {
				t.Fatalf("repository pack carries a non-regular file %s", rel)
			}
			body, err := os.ReadFile(current)
			if err != nil {
				return err
			}
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			return os.WriteFile(target, body, 0o644)
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func plantPackJunk(t *testing.T, root string) {
	t.Helper()
	capsule := filepath.Join(root, "research", "dittobench-coding-datagen", "certification", "v1", "capsules", publicCanaryTaskID)
	for _, junk := range []string{
		filepath.Join("visible", "workspace", "tests", "__pycache__", "test_visible.cpython-312-pytest-8.4.2.pyc"),
		filepath.Join("visible", "workspace", "__pycache__", "app.cpython-312.pyc"),
		filepath.Join("visible", "workspace", ".DS_Store"),
		filepath.Join("visible", "workspace", "app.pyc"),
		filepath.Join("grader", "tests", "__pycache__", "test_regression.cpython-312.pyc"),
		filepath.Join("grader", ".DS_Store"),
	} {
		target := filepath.Join(capsule, junk)
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, []byte("junk"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func appendToFile(name string, suffix string) error {
	handle, err := os.OpenFile(name, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		return err
	}
	_, writeErr := handle.WriteString(suffix)
	return errors.Join(writeErr, handle.Close())
}

func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func repeatHex(value string) string {
	out := make([]byte, 0, 64)
	for len(out) < 64 {
		out = append(out, value...)
	}
	return string(out[:64])
}
