//go:build ignore

// Local operator qualification only. Private input is mounted at runtime, never
// included in the image build context. Output must remain in private work logs.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path"
	"regexp"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggobuild"
	"github.com/ditto-assistant/dittobench-api/internal/codinggooracle"
)

type privateObservation struct {
	Calls, Errors int
	Categories    []string
}
type observedCandidate struct {
	codinggooracle.Candidate
	observation *privateObservation
}

func (c *observedCandidate) Call(ctx context.Context, name string, args []codinggooracle.Value) ([]codinggooracle.Value, error) {
	c.observation.Calls++
	values, err := c.Candidate.Call(ctx, name, args)
	if err != nil {
		c.observation.Errors++
		c.observation.Categories = append(c.observation.Categories, err.Error())
	}
	return values, err
}
func (c *observedCandidate) Method(ctx context.Context, receiver codinggooracle.Value, name string, args []codinggooracle.Value) ([]codinggooracle.Value, error) {
	c.observation.Calls++
	values, err := c.Candidate.Method(ctx, receiver, name, args)
	if err != nil {
		c.observation.Errors++
		c.observation.Categories = append(c.observation.Categories, err.Error())
	}
	return values, err
}

func main() {
	group := flag.String("group", "", "opaque group identifier")
	variant := flag.String("variant", "", "base or reference")
	flag.Parse()
	if !regexp.MustCompile(`^private-group-[0-9]{3}$`).MatchString(*group) || (*variant != "base" && *variant != "reference") {
		os.Exit(70)
	}
	if err := qualify(*group, *variant); err != nil {
		println("private Go qualification failed")
		os.Exit(70)
	}
}

func qualify(group, variant string) error {
	root, err := os.OpenRoot("/private-input")
	if err != nil {
		return codinggobuild.ErrBuild
	}
	defer root.Close()
	base := path.Join("groups", group, "snapshot", "workspace")
	current := base
	if variant == "reference" {
		current = path.Join("groups", group, "curator", "gold-workspace")
	}
	entries, err := fs.ReadDir(root.FS(), base)
	if err != nil {
		return codinggobuild.ErrBuild
	}
	request := codinggobuild.Request{UID: 10001, GID: 10001}
	var candidate []codinggooracle.Source
	var visible []codinggooracle.Source
	inputHash := sha256.New()
	for _, entry := range entries {
		if entry.IsDir() {
			return codinggobuild.ErrBuild
		} // This candidate corpus has flat Go libraries.
		name := entry.Name()
		if strings.HasSuffix(name, "_test.go") {
			body, err := root.ReadFile(path.Join(base, name))
			if err != nil {
				return codinggobuild.ErrBuild
			}
			visible = append(visible, codinggooracle.Source{Name: name, Body: body})
			continue
		}
		body, err := root.ReadFile(path.Join(current, name))
		if err != nil || len(body) > 8<<20 {
			return codinggobuild.ErrBuild
		}
		sum := sha256.Sum256(body)
		inputHash.Write([]byte(name))
		inputHash.Write(sum[:])
		request.Files = append(request.Files, codinggobuild.File{Path: name, Bytes: body})
		if name == "go.mod" {
			for _, line := range strings.Split(string(body), "\n") {
				fields := strings.Fields(line)
				if len(fields) == 2 && fields[0] == "module" {
					request.ModulePath = fields[1]
				}
			}
		}
		if !strings.HasSuffix(name, ".go") {
			continue
		}
		candidate = append(candidate, codinggooracle.Source{Name: name, Body: body})
		// API binding comes from pristine candidate source, not hidden tests or
		// reference-only helpers. Implementation bytes may differ in the reference.
		baseBody, err := root.ReadFile(path.Join(base, name))
		if err != nil {
			return codinggobuild.ErrBuild
		}
		parsed, err := parser.ParseFile(token.NewFileSet(), "candidate.go", baseBody, parser.SkipObjectResolution)
		if err != nil {
			return codinggobuild.ErrBuild
		}
		request.PackageName = parsed.Name.Name
		for _, decl := range parsed.Decls {
			if function, ok := decl.(*ast.FuncDecl); ok && function.Recv == nil && function.Name.Name != "init" && function.Name.Name != "main" {
				request.Functions = append(request.Functions, function.Name.Name)
			}
		}
	}
	if len(candidate) == 0 || len(visible) == 0 {
		return codinggobuild.ErrBuild
	}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Second)
	defer cancel()
	artifact, err := codinggobuild.Build(ctx, request)
	if err != nil {
		return err
	}
	defer artifact.Close()
	var hidden []codinggooracle.Source
	paths, err := fs.Glob(root.FS(), path.Join("groups", group, "grader", "*_test.go"))
	if err != nil || len(paths) == 0 {
		return codinggobuild.ErrBuild
	}
	for _, file := range paths {
		body, err := root.ReadFile(file)
		if err != nil {
			return codinggobuild.ErrBuild
		}
		hidden = append(hidden, codinggooracle.Source{Name: path.Base(file), Body: body})
	}
	totals := map[string]codinggooracle.Result{}
	observations := map[string][]*privateObservation{}
	for _, set := range []struct {
		name  string
		files []codinggooracle.Source
	}{{"visible", visible}, {"hidden", hidden}} {
		for _, source := range set.files {
			parsed, err := parser.ParseFile(token.NewFileSet(), "private_test.go", source.Body, parser.SkipObjectResolution)
			if err != nil {
				return codinggooracle.ErrSuite
			}
			count := 0
			for _, decl := range parsed.Decls {
				if function, ok := decl.(*ast.FuncDecl); ok && strings.HasPrefix(function.Name.Name, "Test") {
					count++
				}
			}
			config := codinggooracle.Config{PackagePath: request.ModulePath, CandidateSources: candidate, Functions: request.Functions, ExpectedTests: count, Importer: artifact.Importer(token.NewFileSet())}
			if set.name == "hidden" {
				config.SupportSources = visible
			}
			suite, err := codinggooracle.Compile(source.Body, config)
			if err != nil {
				return err
			}
			factory := codinggooracle.ProcessFactory(artifact, suite.APISignatures(), 10001, 10001, 5*time.Second)
			result, err := codinggooracle.Run(ctx, suite, func(ctx context.Context) (codinggooracle.Candidate, error) {
				candidate, err := factory(ctx)
				if err != nil {
					return candidate, err
				}
				observation := &privateObservation{}
				observations[set.name] = append(observations[set.name], observation)
				return &observedCandidate{candidate, observation}, nil
			})
			if err != nil || !result.Completed {
				return codinggooracle.ErrRuntime
			}
			previous := totals[set.name]
			previous.Passed += result.Passed
			previous.Total += result.Total
			previous.Completed = true
			totals[set.name] = previous
		}
	}
	output := struct {
		Schema, Group, Variant, InputSHA256, ProgramSHA256 string
		Visible, Hidden                                    codinggooracle.Result
		RuntimeQualification                               bool
		Observations                                       map[string][]*privateObservation
	}{
		"dittobench-go-local-control-v1", group, variant, hex.EncodeToString(inputHash.Sum(nil)), artifact.SHA256, totals["visible"], totals["hidden"], false, observations,
	}
	if err := json.NewEncoder(os.Stdout).Encode(output); err != nil {
		return err
	}
	if output.Visible.Total == 0 || output.Hidden.Total == 0 || output.Visible.Passed != output.Visible.Total {
		return codinggooracle.ErrRuntime
	}
	if variant == "reference" && output.Hidden.Passed != output.Hidden.Total {
		return codinggooracle.ErrRuntime
	}
	if variant == "base" && output.Hidden.Passed >= output.Hidden.Total {
		return codinggooracle.ErrRuntime
	}
	return nil
}
