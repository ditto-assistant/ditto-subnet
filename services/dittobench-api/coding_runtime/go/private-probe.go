//go:build ignore

// Local operator qualification only. Private input is mounted at runtime, never
// included in the image build context. Output must remain in private work logs.
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"os/exec"
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
	phase := flag.String("supervisor-phase", "", "optional visible or hidden supervisor control")
	flag.Parse()
	if !regexp.MustCompile(`^private-group-[0-9]{3}$`).MatchString(*group) || (*variant != "base" && *variant != "reference") || (*phase != "" && *phase != "visible" && *phase != "hidden") {
		os.Exit(70)
	}
	if err := qualify(*group, *variant, *phase); err != nil {
		println("private Go qualification failed")
		os.Exit(70)
	}
}

func qualify(group, variant, phase string) error {
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
	if phase != "" {
		return supervisorControl(root, group, variant, phase, request, visible, hex.EncodeToString(inputHash.Sum(nil)))
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

// supervisorControl deliberately runs one suite in a fresh disposable container.
// It copies only snapshot-authorized files: reference workspaces can contain old
// injected graders and must never be mounted as the candidate workspace.
func supervisorControl(root *os.Root, group, variant, phase string, input codinggobuild.Request, visible []codinggooracle.Source, inputDigest string) error {
	for _, directory := range []string{"/workspace", "/run/dittobench-grader", "/run/dittobench-control"} {
		entries, err := os.ReadDir(directory)
		if err != nil || len(entries) != 0 {
			return codinggooracle.ErrRuntime
		}
	}
	write := func(name string, body []byte) error {
		file, err := os.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0444)
		if err != nil {
			return codinggooracle.ErrRuntime
		}
		_, err = file.Write(body)
		closeErr := file.Close()
		if err != nil || closeErr != nil {
			return codinggooracle.ErrRuntime
		}
		return nil
	}
	for _, file := range input.Files {
		if path.Base(file.Path) != file.Path || write(path.Join("/workspace", file.Path), file.Bytes) != nil {
			return codinggooracle.ErrRuntime
		}
	}
	supportHash := sha256.New()
	for _, file := range visible {
		if write(path.Join("/workspace", file.Name), file.Body) != nil {
			return codinggooracle.ErrRuntime
		}
		digest := sha256.Sum256(file.Body)
		supportHash.Write([]byte(file.Name))
		supportHash.Write(digest[:])
	}
	var selected codinggooracle.Source
	if phase == "visible" {
		if len(visible) != 1 {
			return codinggooracle.ErrSuite
		}
		selected = visible[0]
	} else {
		files, err := fs.Glob(root.FS(), path.Join("groups", group, "grader", "*_test.go"))
		if err != nil || len(files) != 1 {
			return codinggooracle.ErrSuite
		}
		body, err := root.ReadFile(files[0])
		if err != nil || len(body) > 512<<10 {
			return codinggooracle.ErrSuite
		}
		selected = codinggooracle.Source{Name: path.Base(files[0]), Body: body}
		if write(path.Join("/run/dittobench-grader", selected.Name), body) != nil {
			return codinggooracle.ErrRuntime
		}
	}
	parsed, err := parser.ParseFile(token.NewFileSet(), "private_test.go", selected.Body, parser.SkipObjectResolution)
	if err != nil {
		return codinggooracle.ErrSuite
	}
	count := 0
	for _, declaration := range parsed.Decls {
		if function, ok := declaration.(*ast.FuncDecl); ok && function.Recv == nil && strings.HasPrefix(function.Name.Name, "Test") {
			count++
		}
	}
	if count < 1 {
		return codinggooracle.ErrSuite
	}
	argv := []string{"dittobench-test-driver", "--group", phase, "--suite", selected.Name, "--package-path", input.ModulePath, "--candidate-timeout-ms", "5000", "--build-timeout-ms", "120000"}
	for _, name := range input.Functions {
		argv = append(argv, "--function", name)
	}
	if phase == "hidden" {
		for _, file := range visible {
			argv = append(argv, "--support", file.Name)
		}
	}
	command := struct {
		Argv    []string `json:"argv"`
		ID      string   `json:"id"`
		Timeout int      `json:"timeout_milliseconds"`
	}{argv, "private-control", 150000}
	body, err := json.Marshal(command)
	if err != nil {
		return codinggooracle.ErrRuntime
	}
	commandDigest := sha256.Sum256(append(body, '\n'))
	nonce := make([]byte, 24)
	if _, err := rand.Read(nonce); err != nil {
		return codinggooracle.ErrRuntime
	}
	request := map[string]any{"schema": "dittobench-coding-supervisor-request-v1", "nonce": hex.EncodeToString(nonce), "mode": "test", "command_id": command.ID, "command_sha256": hex.EncodeToString(commandDigest[:]), "argv": argv, "timeout_milliseconds": command.Timeout, "expected_total": count, "candidate_uid": 10001, "candidate_gid": 10001}
	body, err = json.Marshal(request)
	if err != nil || write("/run/dittobench-control/request.json", body) != nil {
		return codinggooracle.ErrRuntime
	}
	requestDigest := sha256.Sum256(body)
	ctx, cancel := context.WithTimeout(context.Background(), 160*time.Second)
	defer cancel()
	commandRun := exec.CommandContext(ctx, "/usr/local/bin/dittobench-coding-supervisor", "--request", "/run/dittobench-control/request.json", "--response", "/run/dittobench-control/response.json")
	if commandRun.Run() != nil {
		return codinggooracle.ErrRuntime
	}
	body, err = os.ReadFile("/run/dittobench-control/response.json")
	if err != nil {
		return codinggooracle.ErrRuntime
	}
	responseDigest := sha256.Sum256(body)
	var response struct {
		Passed, Total   int
		Completed       bool
		ProcessTreeDead bool `json:"process_tree_dead"`
		Stdout, Stderr  string
	}
	if json.Unmarshal(body, &response) != nil || !response.Completed || !response.ProcessTreeDead || response.Total != count || response.Stdout != "" || response.Stderr != "" || response.Passed < 0 || response.Passed > count {
		return codinggooracle.ErrRuntime
	}
	if (variant == "reference" || phase == "visible") && response.Passed != count || variant == "base" && phase == "hidden" && response.Passed == count {
		return codinggooracle.ErrRuntime
	}
	suiteDigest := sha256.Sum256(selected.Body)
	return json.NewEncoder(os.Stdout).Encode(struct {
		Schema, Group, Variant, Phase                                                         string
		InputSHA256, SuiteSHA256, SupportSHA256, CommandSHA256, RequestSHA256, ResponseSHA256 string
		Passed, Total                                                                         int
		Completed, ProcessTreeDead, RuntimeQualification                                      bool
	}{"dittobench-go-supervisor-local-control-v1", group, variant, phase, inputDigest, hex.EncodeToString(suiteDigest[:]), hex.EncodeToString(supportHash.Sum(nil)), hex.EncodeToString(commandDigest[:]), hex.EncodeToString(requestDigest[:]), hex.EncodeToString(responseDigest[:]), response.Passed, response.Total, true, true, false})
}
