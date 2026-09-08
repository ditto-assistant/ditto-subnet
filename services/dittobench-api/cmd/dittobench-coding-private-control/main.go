// Public operator helper. Mount it and private inputs only at control runtime;
// never install this helper or private fixtures in an approved runtime image.
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"golang.org/x/sys/unix"
)

var rejected = errors.New("private compatibility control rejected")

type fileInput struct {
	Path   string  `json:"path"`
	Source string  `json:"source,omitempty"`
	Inline *string `json:"inline,omitempty"`
	SHA256 string  `json:"sha256"`
}
type controlCase struct {
	Schema      string      `json:"schema"`
	Language    string      `json:"language"`
	Role        string      `json:"role"`
	Phase       string      `json:"phase"`
	Expected    uint32      `json:"expected_total"`
	Argv        []string    `json:"argv"`
	Workspace   []fileInput `json:"workspace"`
	Grader      []fileInput `json:"grader"`
	RustFiles   []string    `json:"rust_files,omitempty"`
	ImageSHA256 string      `json:"image_sha256"`
}

func hash(body []byte) string { value := sha256.Sum256(body); return hex.EncodeToString(value[:]) }
func digest(value string) bool {
	bytes, err := hex.DecodeString(value)
	return err == nil && len(bytes) == 32 && hex.EncodeToString(bytes) == value
}
func relative(value string) bool {
	if value == "" || len(value) > 240 || path.IsAbs(value) || path.Clean(value) != value || strings.ContainsAny(value, "\\\x00\r\n") {
		return false
	}
	for _, part := range strings.Split(value, "/") {
		if part == ".." || part == "." || part == "" {
			return false
		}
	}
	return true
}
func read(root, name string, maximum int64) ([]byte, error) {
	if !relative(name) {
		return nil, rejected
	}
	fd, err := unix.Open(root, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, rejected
	}
	parts := strings.Split(name, "/")
	for i, part := range parts {
		flags := unix.O_RDONLY | unix.O_NOFOLLOW | unix.O_NONBLOCK | unix.O_CLOEXEC
		if i+1 < len(parts) {
			flags |= unix.O_DIRECTORY
		}
		next, openErr := unix.Openat(fd, part, flags, 0)
		unix.Close(fd)
		if openErr != nil {
			return nil, rejected
		}
		fd = next
	}
	file := os.NewFile(uintptr(fd), "protected-control-input")
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > maximum {
		return nil, rejected
	}
	body, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(body)) != info.Size() {
		return nil, rejected
	}
	return body, nil
}
func write(root, name string, body []byte) error {
	if !relative(name) {
		return rejected
	}
	target := filepath.Join(root, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
		return rejected
	}
	file, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL|unix.O_NOFOLLOW, 0400)
	if err != nil {
		return rejected
	}
	_, err = file.Write(body)
	syncErr := file.Sync()
	closeErr := file.Close()
	return errors.Join(err, syncErr, closeErr)
}
func copyFiles(root string, inputs []fileInput) (map[string]string, error) {
	if len(inputs) > 2048 {
		return nil, rejected
	}
	result := map[string]string{}
	total := 0
	for _, input := range inputs {
		if !relative(input.Path) || !digest(input.SHA256) || (input.Source == "") == (input.Inline == nil) {
			return nil, rejected
		}
		if _, ok := result[input.Path]; ok {
			return nil, rejected
		}
		var body []byte
		var err error
		if input.Inline != nil {
			body, err = base64.StdEncoding.Strict().DecodeString(*input.Inline)
		} else {
			body, err = read("/private-input", input.Source, 8<<20)
		}
		total += len(body)
		if err != nil || len(body) > 8<<20 || total > 32<<20 || hash(body) != input.SHA256 {
			return nil, rejected
		}
		if err := write(root, input.Path, body); err != nil {
			return nil, rejected
		}
		result[input.Path] = input.SHA256
	}
	return result, nil
}
func run() error {
	if os.Getuid() != 0 || os.Geteuid() != 0 {
		return rejected
	}
	body, err := read("/private-control", "case.json", 2<<20)
	if err != nil || codingcontract.ValidateJSONDocument(body, 2<<20) != nil {
		return rejected
	}
	var c controlCase
	if json.Unmarshal(body, &c) != nil || c.Schema != "dittobench-private-compatibility-case-v1" || !digest(c.ImageSHA256) || c.Expected == 0 || c.Expected > 1024 || len(c.Argv) == 0 || c.Argv[0] != "dittobench-test-driver" || len(c.Argv) > 128 || (c.Role != "base" && c.Role != "reference") || (c.Phase != "visible" && c.Phase != "hidden") {
		return rejected
	}
	if c.Language != "python" && c.Language != "node" && c.Language != "go" && c.Language != "rust" {
		return rejected
	}
	for _, root := range []string{"/workspace", "/run/dittobench-grader", "/run/dittobench-control"} {
		if os.MkdirAll(root, 0700) != nil || os.Chmod(root, 0700) != nil {
			return rejected
		}
	}
	workspace, err := copyFiles("/workspace", c.Workspace)
	if err != nil {
		return rejected
	}
	grader, err := copyFiles("/run/dittobench-grader", c.Grader)
	if err != nil {
		return rejected
	}
	// Public workspace implementation/test inputs must be readable by interpreted
	// candidates. The hidden grader and control roots remain private in all cases.
	if c.Language != "rust" {
		if os.Chmod("/workspace", 0755) != nil {
			return rejected
		}
		for name := range workspace {
			if os.Chmod(filepath.Join("/workspace", name), 0444) != nil {
				return rejected
			}
		}
	}
	nonce := make([]byte, 24)
	if _, err := rand.Read(nonce); err != nil {
		return rejected
	}
	const timeout = 180000
	commandSHA, err := codinggrader.CommandSHA256("private-compatibility", c.Argv, timeout)
	if err != nil {
		return rejected
	}
	request := map[string]any{"schema": "dittobench-coding-supervisor-request-v1", "nonce": hex.EncodeToString(nonce), "mode": "test", "command_id": "private-compatibility", "command_sha256": commandSHA, "argv": c.Argv, "timeout_milliseconds": timeout, "expected_total": c.Expected, "candidate_uid": 10001, "candidate_gid": 10001}
	if c.Language == "rust" {
		var authority string
		for i := 1; i+1 < len(c.Argv); i += 2 {
			if c.Argv[i] == "--authority-sha256" {
				authority = c.Argv[i+1]
			}
		}
		if !digest(authority) || len(c.RustFiles) == 0 || len(c.RustFiles) > 256 {
			return rejected
		}
		sort.Strings(c.RustFiles)
		files := []map[string]string{}
		for i, name := range c.RustFiles {
			value, ok := workspace[name]
			if !ok || (i > 0 && c.RustFiles[i-1] == name) {
				return rejected
			}
			files = append(files, map[string]string{"path": name, "sha256": value})
		}
		frozen, err := json.Marshal(map[string]any{"schema": "dittobench-coding-rust-inputs-v1", "authority_sha256": authority, "image_sha256": c.ImageSHA256, "files": files})
		if err != nil || write("/run/dittobench-control", "rust-inputs.json", frozen) != nil {
			return rejected
		}
		request["rust"] = map[string]string{"authority_sha256": authority, "inputs_sha256": hash(frozen), "image_sha256": c.ImageSHA256}
	}
	encoded, err := json.Marshal(request)
	if err != nil || write("/run/dittobench-control", "request.json", encoded) != nil {
		return rejected
	}
	ctx, cancel := context.WithTimeout(context.Background(), 190*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/local/bin/dittobench-coding-supervisor", "--request", "/run/dittobench-control/request.json", "--response", "/run/dittobench-control/response.json")
	command.Env = []string{"PATH=/usr/local/bin:/usr/bin:/bin"}
	command.Stdout = io.Discard
	command.Stderr = io.Discard
	if command.Run() != nil {
		return rejected
	}
	response, err := read("/run/dittobench-control", "response.json", 65536)
	if err != nil {
		return rejected
	}
	var r struct {
		Nonce     string `json:"nonce"`
		Passed    uint32 `json:"passed"`
		Total     uint32 `json:"total"`
		Completed bool   `json:"completed"`
		Dead      bool   `json:"process_tree_dead"`
		TimedOut  bool   `json:"timed_out"`
		Stdout    string `json:"stdout"`
		Stderr    string `json:"stderr"`
	}
	if json.Unmarshal(response, &r) != nil || r.Nonce != hex.EncodeToString(nonce) || !r.Completed || !r.Dead || r.TimedOut || r.Total != c.Expected || r.Passed > r.Total || r.Stdout != "" || r.Stderr != "" {
		return rejected
	}
	matched := r.Passed == r.Total
	if c.Role == "base" && c.Phase == "hidden" {
		matched = r.Passed < r.Total
	}
	w, _ := json.Marshal(workspace)
	g, _ := json.Marshal(grader)
	observation := map[string]any{"schema": "dittobench-private-compatibility-observation-v1", "case_sha256": hash(body), "language": c.Language, "role": c.Role, "phase": c.Phase, "passed": r.Passed, "total": r.Total, "completed": true, "process_tree_dead": true, "expectation_matched": matched, "workspace_sha256": hash(w), "grader_sha256": hash(g), "response_sha256": hash(response), "runtime_qualification": false, "production_api_approval": false}
	observation["supervisor_response"] = json.RawMessage(response)
	if json.NewEncoder(os.Stdout).Encode(observation) != nil || !matched {
		return rejected
	}
	return nil
}
func inspectGo(root, suite string) error {
	mod, err := read(root, "go.mod", 65536)
	if err != nil || len(mod) > 65536 {
		return rejected
	}
	module := ""
	for _, line := range strings.Split(string(mod), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[0] == "module" {
			module = fields[1]
		}
	}
	functions := []string{}
	entries, err := os.ReadDir(root)
	if err != nil {
		return rejected
	}
	for _, entry := range entries {
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".go") && !strings.HasSuffix(entry.Name(), "_test.go") {
			body, err := read(root, entry.Name(), 1<<20)
			if err != nil {
				return err
			}
			file, err := parser.ParseFile(token.NewFileSet(), "snapshot.go", body, parser.SkipObjectResolution)
			if err != nil {
				return rejected
			}
			for _, decl := range file.Decls {
				if f, ok := decl.(*ast.FuncDecl); ok && f.Recv == nil && f.Name.Name != "main" && f.Name.Name != "init" {
					functions = append(functions, f.Name.Name)
				}
			}
		}
	}
	suiteBody, err := read(filepath.Dir(suite), filepath.Base(suite), 512<<10)
	if err != nil {
		return rejected
	}
	file, err := parser.ParseFile(token.NewFileSet(), "private_suite.go", suiteBody, parser.SkipObjectResolution)
	if err != nil {
		return rejected
	}
	count := 0
	for _, decl := range file.Decls {
		if f, ok := decl.(*ast.FuncDecl); ok && strings.HasPrefix(f.Name.Name, "Test") {
			count++
		}
	}
	if module == "" || count == 0 || len(functions) == 0 {
		return rejected
	}
	sort.Strings(functions)
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"module_path": module, "functions": functions, "expected_total": count, "production_api_approval": false})
}
func main() {
	var err error
	if len(os.Args) == 4 && os.Args[1] == "--inspect-go" {
		err = inspectGo(os.Args[2], os.Args[3])
	} else if len(os.Args) == 1 {
		err = run()
	} else {
		err = rejected
	}
	if err != nil {
		os.Stderr.WriteString("private compatibility control failed\n")
		os.Exit(70)
	}
}
