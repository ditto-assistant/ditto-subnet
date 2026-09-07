// Package codinggodriver connects protected Go suites to the trusted supervisor
// report ABI. It does not approve a runtime, private release or scoring route.
package codinggodriver

import (
	"context"
	"encoding/json"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codinggobuild"
	"github.com/ditto-assistant/dittobench-api/internal/codinggooracle"
)

var ErrDriver = errors.New("private Go driver rejected")

const reportPath = "/run/dittobench-control/test-report.json"

type Options struct {
	Group, Suite, PackagePath, Nonce, Report string
	Functions, Support                       []string
	UID, GID                                 uint32
	Expected                                 int
	CandidateTimeout, BuildTimeout           time.Duration
}

func (Options) String() string               { return "<private Go driver options>" }
func (Options) GoString() string             { return "<private Go driver options>" }
func (Options) MarshalJSON() ([]byte, error) { return nil, ErrDriver }

func Parse(arguments []string) (Options, error) {
	values := map[string]string{}
	options := Options{BuildTimeout: 120 * time.Second}
	allowed := map[string]bool{"group": true, "suite": true, "package-path": true, "candidate-timeout-ms": true, "build-timeout-ms": true, "dittobench-report": true, "dittobench-nonce": true, "dittobench-expected": true, "dittobench-candidate-uid": true, "dittobench-candidate-gid": true}
	for i := 0; i < len(arguments); i += 2 {
		if i+1 >= len(arguments) || !strings.HasPrefix(arguments[i], "--") {
			return Options{}, ErrDriver
		}
		key, value := strings.TrimPrefix(arguments[i], "--"), arguments[i+1]
		switch key {
		case "function":
			options.Functions = append(options.Functions, value)
		case "support":
			options.Support = append(options.Support, value)
		default:
			if !allowed[key] {
				return Options{}, ErrDriver
			}
			if _, ok := values[key]; ok {
				return Options{}, ErrDriver
			}
			values[key] = value
		}
	}
	for key := range allowed {
		if key != "build-timeout-ms" && values[key] == "" {
			return Options{}, ErrDriver
		}
	}
	options.Group = values["group"]
	options.Suite = values["suite"]
	options.PackagePath = values["package-path"]
	options.Nonce = values["dittobench-nonce"]
	options.Report = values["dittobench-report"]
	if (options.Group != "visible" && options.Group != "hidden") || options.Report != reportPath || !regexp.MustCompile(`^[0-9a-f]{48}$`).MatchString(options.Nonce) || len(options.Functions) == 0 || len(options.Functions) > 256 || len(options.Support) > 16 {
		return Options{}, ErrDriver
	}
	functions := map[string]bool{}
	for _, name := range options.Functions {
		if !token.IsIdentifier(name) || name == "_" || name == "init" || name == "main" || functions[name] {
			return Options{}, ErrDriver
		}
		functions[name] = true
	}
	for _, relative := range append([]string{options.Suite}, options.Support...) {
		if !relativePath(relative) || !strings.HasSuffix(relative, "_test.go") {
			return Options{}, ErrDriver
		}
	}
	number := func(key string, max uint64) (uint64, error) {
		text := values[key]
		if !regexp.MustCompile(`^[1-9][0-9]*$`).MatchString(text) {
			return 0, ErrDriver
		}
		value, err := strconv.ParseUint(text, 10, 64)
		if err != nil || value > max {
			return 0, ErrDriver
		}
		return value, nil
	}
	uid, err := number("dittobench-candidate-uid", uint64(^uint32(0)-1))
	if err != nil {
		return Options{}, err
	}
	gid, err := number("dittobench-candidate-gid", uint64(^uint32(0)-1))
	if err != nil {
		return Options{}, err
	}
	expected, err := number("dittobench-expected", 1024)
	if err != nil {
		return Options{}, err
	}
	timeout, err := number("candidate-timeout-ms", 300000)
	if err != nil {
		return Options{}, err
	}
	options.UID = uint32(uid)
	options.GID = uint32(gid)
	options.Expected = int(expected)
	options.CandidateTimeout = time.Duration(timeout) * time.Millisecond
	if values["build-timeout-ms"] != "" {
		timeout, err := number("build-timeout-ms", 300000)
		if err != nil {
			return Options{}, err
		}
		options.BuildTimeout = time.Duration(timeout) * time.Millisecond
	}
	return options, nil
}
func relativePath(name string) bool {
	return name != "" && len(name) <= 240 && !path.IsAbs(name) && path.Clean(name) == name && name != ".." && !strings.HasPrefix(name, "../") && !strings.ContainsAny(name, "\\\x00\n\r")
}

func protectedDirectory(name string) error {
	canonical, err := filepath.EvalSymlinks(name)
	if err != nil || canonical != name {
		return ErrDriver
	}
	info, err := os.Lstat(name)
	if err != nil || !info.IsDir() || info.Mode().Perm() != 0700 || info.Mode()&(os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 {
		return ErrDriver
	}
	if data, ok := info.Sys().(*syscall.Stat_t); !ok || data.Uid != 0 {
		return ErrDriver
	}
	return nil
}
func readFile(root *os.Root, name string, maximum int64) ([]byte, error) {
	if !relativePath(name) {
		return nil, ErrDriver
	}
	file, err := root.OpenFile(name, os.O_RDONLY|syscall.O_NONBLOCK|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, ErrDriver
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > maximum {
		return nil, ErrDriver
	}
	body, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(body)) > maximum {
		return nil, ErrDriver
	}
	return body, nil
}
func testCount(source []byte) (int, error) {
	file, err := parser.ParseFile(token.NewFileSet(), "private_test.go", source, parser.SkipObjectResolution)
	if err != nil {
		return 0, ErrDriver
	}
	count := 0
	for _, decl := range file.Decls {
		if function, ok := decl.(*ast.FuncDecl); ok && strings.HasPrefix(function.Name.Name, "Test") {
			tail := strings.TrimPrefix(function.Name.Name, "Test")
			r, _ := utf8.DecodeRuneInString(tail)
			if tail == "" || !unicode.IsLower(r) {
				count++
			}
		}
	}
	return count, nil
}

type loaded struct {
	source             []byte
	support, candidate []codinggooracle.Source
	request            codinggobuild.Request
}

func load(options Options) (loaded, error) {
	var input loaded
	workspace, err := os.OpenRoot("/workspace")
	if err != nil {
		return input, ErrDriver
	}
	defer workspace.Close()
	grader, err := os.OpenRoot("/run/dittobench-grader")
	if err != nil {
		return input, ErrDriver
	}
	defer grader.Close()
	suiteRoot := workspace
	if options.Group == "hidden" {
		suiteRoot = grader
	}
	input.source, err = readFile(suiteRoot, options.Suite, 512<<10)
	if err != nil {
		return input, err
	}
	count, err := testCount(input.source)
	if err != nil || count != options.Expected {
		return input, ErrDriver
	}
	seen := map[string]bool{}
	for _, name := range options.Support {
		if seen[name] || (options.Group == "visible" && name == options.Suite) {
			return input, ErrDriver
		}
		seen[name] = true
		body, err := readFile(workspace, name, 512<<10)
		if err != nil {
			return input, err
		}
		input.support = append(input.support, codinggooracle.Source{Name: name, Body: body})
	}
	input.request = codinggobuild.Request{ModulePath: options.PackagePath, Functions: options.Functions, UID: options.UID, GID: options.GID}
	total, files := 0, 0
	err = fs.WalkDir(workspace.FS(), ".", func(name string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return ErrDriver
		}
		if name == "." {
			return nil
		}
		if !relativePath(name) || entry.Type()&os.ModeSymlink != 0 {
			return ErrDriver
		}
		if entry.IsDir() {
			return nil
		}
		if strings.HasSuffix(name, "_test.go") {
			return nil
		}
		body, err := readFile(workspace, name, 8<<20)
		if err != nil {
			return err
		}
		total += len(body)
		files++
		if total > 32<<20 || files > 2048 {
			return ErrDriver
		}
		input.request.Files = append(input.request.Files, codinggobuild.File{Path: name, Bytes: body})
		if path.Dir(name) == "." && strings.HasSuffix(name, ".go") {
			file, err := parser.ParseFile(token.NewFileSet(), "candidate.go", body, parser.PackageClauseOnly)
			if err != nil {
				return codinggobuild.ErrCompilation
			}
			if input.request.PackageName != "" && input.request.PackageName != file.Name.Name {
				return codinggobuild.ErrCompilation
			}
			input.request.PackageName = file.Name.Name
			input.candidate = append(input.candidate, codinggooracle.Source{Name: name, Body: body})
		}
		return nil
	})
	if err != nil {
		return input, err
	}
	if len(input.candidate) == 0 {
		return input, codinggobuild.ErrCompilation
	}
	return input, nil
}

func writeReport(options Options, result codinggooracle.Result) error {
	if !result.Completed || result.Total != options.Expected || result.Passed < 0 || result.Passed > result.Total {
		return ErrDriver
	}
	report := struct {
		Schema    string `json:"schema"`
		Nonce     string `json:"nonce"`
		Passed    int    `json:"passed"`
		Total     int    `json:"total"`
		Completed bool   `json:"completed"`
	}{"dittobench-coding-trusted-test-report-v1", options.Nonce, result.Passed, result.Total, true}
	file, err := os.OpenFile(options.Report, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return ErrDriver
	}
	encodeErr := json.NewEncoder(file).Encode(report)
	syncErr := file.Sync()
	closeErr := file.Close()
	if encodeErr != nil || syncErr != nil || closeErr != nil {
		return ErrDriver
	}
	return nil
}

// Run returns the conventional trusted-driver exit status. It never prints
// private source, diagnostics, output, filenames or candidate values.
func Run(ctx context.Context, arguments []string) (int, error) {
	options, err := Parse(arguments)
	if err != nil {
		return 70, err
	}
	if os.Geteuid() != 0 || protectedDirectory("/run/dittobench-control") != nil || protectedDirectory("/run/dittobench-grader") != nil {
		return 70, ErrDriver
	}
	if _, err := os.Lstat(options.Report); !errors.Is(err, os.ErrNotExist) {
		return 70, ErrDriver
	}
	input, err := load(options)
	failed := codinggooracle.Result{Total: options.Expected, Completed: true}
	if errors.Is(err, codinggobuild.ErrCompilation) {
		if writeReport(options, failed) != nil {
			return 70, ErrDriver
		}
		return 1, nil
	}
	if err != nil {
		return 70, err
	}
	buildContext, cancel := context.WithTimeout(ctx, options.BuildTimeout)
	artifact, err := codinggobuild.Build(buildContext, input.request)
	cancel()
	if errors.Is(err, codinggobuild.ErrCompilation) {
		if writeReport(options, failed) != nil {
			return 70, ErrDriver
		}
		return 1, nil
	}
	if err != nil {
		return 70, ErrDriver
	}
	defer artifact.Close()
	suite, err := codinggooracle.Compile(input.source, codinggooracle.Config{PackagePath: options.PackagePath, CandidateSources: input.candidate, SupportSources: input.support, Functions: options.Functions, ExpectedTests: options.Expected, Importer: artifact.Importer(token.NewFileSet())})
	if errors.Is(err, codinggooracle.ErrCandidateTypes) {
		if artifact.Close() != nil || writeReport(options, failed) != nil {
			return 70, ErrDriver
		}
		return 1, nil
	}
	if err != nil {
		return 70, ErrDriver
	}
	result, err := codinggooracle.Run(ctx, suite, codinggooracle.ProcessFactory(artifact, suite.APISignatures(), options.UID, options.GID, options.CandidateTimeout))
	if err != nil {
		return 70, ErrDriver
	}
	if artifact.Close() != nil || writeReport(options, result) != nil {
		return 70, ErrDriver
	}
	if result.Passed != result.Total {
		return 1, nil
	}
	return 0, nil
}
