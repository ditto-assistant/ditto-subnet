package codinggooracle

import (
	"context"
	"crypto/sha256"
	"fmt"
	"go/ast"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"io"
	"io/fs"
	"os"
	"os/exec"
	iopath "path"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"testing"
	"time"
)

type standardOnlyImporter struct {
	known    map[string]bool
	delegate types.Importer
}

func (i standardOnlyImporter) Import(name string) (*types.Package, error) {
	if !i.known[name] {
		return nil, fmt.Errorf("nonstandard audit import unavailable")
	}
	return i.delegate.Import(name)
}

// Opt-in, read-only compatibility audit. No private bytes, paths, identifiers,
// literals or diagnostic details enter logs or repository files. This checks
// admission only, not base/reference outcomes or runtime qualification.
func TestLocalPrivateCorpusAdmission(t *testing.T) {
	root := os.Getenv("DITTO_CODING_PRIVATE_AUDIT_ROOT")
	if root == "" {
		t.Skip("local protected corpus audit is opt-in")
	}
	absolute, err := filepath.Abs(root)
	if err != nil {
		t.Fatal("private audit root unavailable")
	}
	canonical, err := filepath.EvalSymlinks(absolute)
	if err != nil || canonical != absolute {
		t.Fatal("private audit root is not canonical")
	}
	info, err := os.Stat(absolute)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		t.Fatal("private audit root is not protected")
	}
	rootFiles, err := os.OpenRoot(absolute)
	if err != nil {
		t.Fatal("private audit root unavailable")
	}
	defer rootFiles.Close()
	inputHash := sha256.New()
	readPrivate := func(name string) ([]byte, error) {
		file, err := rootFiles.OpenFile(name, os.O_RDONLY|syscall.O_NONBLOCK, 0)
		if err != nil {
			return nil, err
		}
		defer file.Close()
		info, err := file.Stat()
		if err != nil || !info.Mode().IsRegular() || info.Size() > 8<<20 {
			return nil, fmt.Errorf("private file bound rejected")
		}
		body, err := io.ReadAll(io.LimitReader(file, (8<<20)+1))
		if err != nil || len(body) > 8<<20 {
			return nil, fmt.Errorf("private file read rejected")
		}
		sum := sha256.Sum256(body)
		inputHash.Write([]byte(name))
		inputHash.Write([]byte{0})
		inputHash.Write(sum[:])
		return body, nil
	}
	// Only public standard-library names may reach the fallback metadata loader.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	standard, err := exec.CommandContext(ctx, "go", "list", "std").Output()
	if err != nil {
		t.Fatal("standard type inventory unavailable")
	}
	known := map[string]bool{}
	for _, name := range strings.Fields(string(standard)) {
		known[name] = true
	}
	loader := standardOnlyImporter{known, importer.Default()}
	groups, err := fs.ReadDir(rootFiles.FS(), "groups")
	if err != nil {
		t.Fatal("private group inventory unavailable")
	}
	accepted, rejected, goGroups := 0, 0, 0
	reasons := map[string]int{}
	setHash := sha256.New()
	for _, group := range groups {
		if !group.IsDir() {
			continue
		}
		workspace := iopath.Join("groups", group.Name(), "snapshot", "workspace")
		moduleBytes, err := readPrivate(iopath.Join(workspace, "go.mod"))
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			t.Fatal("private module unavailable")
		}
		module := ""
		for _, line := range strings.Split(string(moduleBytes), "\n") {
			fields := strings.Fields(line)
			if len(fields) == 2 && fields[0] == "module" {
				module = fields[1]
			}
		}
		if module == "" {
			t.Fatal("private module identity unavailable")
		}
		config := Config{PackagePath: module, Importer: loader}
		paths, err := fs.Glob(rootFiles.FS(), iopath.Join(workspace, "*.go"))
		if err != nil {
			t.Fatal("private source inventory unavailable")
		}
		var suites []string
		for _, path := range paths {
			if strings.HasSuffix(path, "_test.go") {
				suites = append(suites, path)
				continue
			}
			body, err := readPrivate(path)
			if err != nil {
				t.Fatal("private source unavailable")
			}
			config.CandidateSources = append(config.CandidateSources, Source{Name: filepath.Base(path), Body: body})
			file, err := parser.ParseFile(token.NewFileSet(), "candidate.go", body, parser.SkipObjectResolution)
			if err != nil {
				t.Fatal("private source syntax invalid")
			}
			for _, decl := range file.Decls {
				function, ok := decl.(*ast.FuncDecl)
				if !ok || function.Name.Name == "init" || function.Name.Name == "main" {
					continue
				}
				if function.Recv == nil {
					config.Functions = append(config.Functions, function.Name.Name)
					continue
				}
				receiver := function.Recv.List[0].Type
				if pointer, ok := receiver.(*ast.StarExpr); ok {
					receiver = pointer.X
				}
				name, ok := receiver.(*ast.Ident)
				if !ok {
					t.Fatal("private receiver shape unsupported by audit")
				}
				config.Methods = append(config.Methods, name.Name+"."+function.Name.Name)
			}
		}
		if len(config.CandidateSources) == 0 {
			t.Fatal("private Go sources missing")
		}
		goGroups++
		visible := append([]string(nil), suites...)
		hidden, err := fs.Glob(rootFiles.FS(), iopath.Join("groups", group.Name(), "grader", "*_test.go"))
		if err != nil || len(hidden) == 0 || len(visible) == 0 {
			t.Fatal("private grader inventory unavailable")
		}
		suites = append(suites, hidden...)
		sort.Strings(suites)
		for _, path := range suites {
			config.SupportSources = nil
			if strings.Contains(path, "/grader/") {
				for _, support := range visible {
					body, err := readPrivate(support)
					if err != nil {
						t.Fatal("protected helper source unavailable")
					}
					config.SupportSources = append(config.SupportSources, Source{Name: filepath.Base(support), Body: body})
				}
			}
			body, err := readPrivate(path)
			if err != nil {
				t.Fatal("private suite unavailable")
			}
			hash := sha256.Sum256(body)
			setHash.Write(hash[:])
			file, err := parser.ParseFile(token.NewFileSet(), "private_test.go", body, parser.SkipObjectResolution)
			if err != nil {
				t.Fatal("private suite syntax invalid")
			}
			config.ExpectedTests = 0
			for _, decl := range file.Decls {
				if function, ok := decl.(*ast.FuncDecl); ok && isTest(function.Name.Name) {
					config.ExpectedTests++
				}
			}
			if _, err := Compile(body, config); err != nil {
				rejected++
				switch err {
				case ErrSuite:
					reasons["unsupported_syntax_or_policy"]++
				case ErrCandidateTypes:
					reasons["type_mismatch"]++
				default:
					reasons["other"]++
				}
			} else {
				accepted++
			}
		}
	}
	t.Logf("private structural audit: groups=%d suites=%d accepted=%d rejected=%d set_sha256=%x input_sha256=%x reasons=%v", goGroups, accepted+rejected, accepted, rejected, setHash.Sum(nil), inputHash.Sum(nil), reasons)
	if goGroups == 0 || rejected != 0 {
		t.Fatal("private suite admission is incomplete; no runtime qualification claimed")
	}
}
