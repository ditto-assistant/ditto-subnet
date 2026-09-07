//go:build ignore

// Public synthetic image fixture; absent from the production target.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
)

func need(ok bool) {
	if !ok {
		panic("Go supervisor probe failed")
	}
}
func main() {
	scenario := os.Args[1]
	attacks := map[string]string{
		"pass": "", "visible": "", "wrong": "return 99", "stdout": `fmt.Println("ordinary output")`,
		"exit": "os.Exit(0)", "hang": "for { runtime.Gosched() }", "init-exit": "", "init-spawn": "",
		"private-read": `if _,err:=os.ReadFile("/run/dittobench-grader/hidden_test.go");!errors.Is(err,syscall.EACCES){return 99}`,
		"fake-report":  `if err:=os.WriteFile("/run/dittobench-control/test-report.json",[]byte("{}"),0600);!errors.Is(err,syscall.EACCES){return 99};fmt.Println("{\"passed\":2,\"total\":2}");return 99`,
		"fake-api":     `file:=os.NewFile(5,"api");file.Write([]byte("{\"id\":\"wrong\",\"values\":[]}\n"));return 99`,
		"oversized":    `file:=os.NewFile(5,"api");file.Write(bytes.Repeat([]byte("x"),70000));return 99`,
		"compile-fail": "", "compiler-private-read": "", "unsupported": "", "count-mismatch": "",
	}
	attack, ok := attacks[scenario]
	need(ok)
	for _, directory := range []string{"/workspace", "/run/dittobench-grader", "/run/dittobench-control"} {
		need(os.MkdirAll(directory, 0700) == nil)
	}
	need(os.Chmod("/workspace", 0755) == nil)
	need(os.Chmod("/run/dittobench-grader", 0700) == nil)
	need(os.Chmod("/run/dittobench-control", 0700) == nil)
	need(os.WriteFile("/workspace/go.mod", []byte("module example.invalid/subject\ngo 1.26.6\n"), 0444) == nil)
	initBody := ""
	if scenario == "init-exit" {
		initBody = "os.Exit(0)"
	}
	if scenario == "init-spawn" {
		initBody = `if !errors.Is(exec.Command("/bin/true").Run(),syscall.EPERM){panic("unconfined init")}`
	}
	source := `package subject
import("os";"os/exec";"errors";"syscall";"fmt";"runtime";"bytes")
var _=os.Getuid;var _=exec.Command;var _=errors.Is;var _=syscall.EACCES;var _=fmt.Println;var _=runtime.Gosched;var _=bytes.Repeat
func init(){` + initBody + `}
func Add(a,b int)int{` + attack + `;return a+b}
func Length(values []string)int{return len(values)}
`
	if scenario == "compile-fail" {
		source = "package subject\nfunc syntax failure\n"
	}
	if scenario == "compiler-private-read" {
		source = "package subject\nfunc Add(a,b int)int\nfunc Length(values []string)int{return len(values)}\n"
		need(os.WriteFile("/workspace/unsafe.s", []byte("#include \"/run/dittobench-grader/hidden_test.go\"\n"), 0444) == nil)
	}
	need(os.WriteFile("/workspace/subject.go", []byte(source), 0444) == nil)
	suite := `package subject;import "testing"
func TestAdd(t *testing.T){if Add(2,3)!=5{t.Fatal("synthetic assertion")}}
func TestNil(t *testing.T){if Length(nil)!=0{t.Fatal("synthetic nil assertion")}}
`
	if scenario == "unsupported" {
		suite = `package subject;import "testing";func TestAdd(t *testing.T){t.Skip("unsupported")};func TestNil(t *testing.T){if Length(nil)!=0{t.Fatal("bad")}}`
	}
	need(os.WriteFile("/workspace/visible_test.go", []byte(suite), 0444) == nil)
	need(os.WriteFile("/run/dittobench-grader/hidden_test.go", []byte(suite), 0444) == nil)
	group, file := "hidden", "hidden_test.go"
	if scenario == "visible" {
		group, file = "visible", "visible_test.go"
	}
	argv := []string{"dittobench-test-driver", "--group", group, "--suite", file, "--package-path", "example.invalid/subject", "--function", "Add", "--function", "Length", "--candidate-timeout-ms", "1000", "--build-timeout-ms", "120000"}
	command := struct {
		Argv    []string `json:"argv"`
		ID      string   `json:"id"`
		Timeout int      `json:"timeout_milliseconds"`
	}{argv, "synthetic", 150000}
	body, err := json.Marshal(command)
	need(err == nil)
	digest := sha256.Sum256(append(body, '\n'))
	expected := 2
	if scenario == "count-mismatch" {
		expected = 3
	}
	request := map[string]any{"schema": "dittobench-coding-supervisor-request-v1", "nonce": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "mode": "test", "command_id": "synthetic", "command_sha256": hex.EncodeToString(digest[:]), "argv": argv, "timeout_milliseconds": 150000, "expected_total": expected, "candidate_uid": 10001, "candidate_gid": 10001}
	body, err = json.Marshal(request)
	need(err == nil)
	need(os.WriteFile("/run/dittobench-control/request.json", body, 0400) == nil)
	run := exec.Command("/usr/local/bin/dittobench-coding-supervisor", "--request", "/run/dittobench-control/request.json", "--response", "/run/dittobench-control/response.json")
	err = run.Run()
	if scenario == "unsupported" || scenario == "count-mismatch" {
		need(err != nil)
		_, statErr := os.Stat("/run/dittobench-control/test-report.json")
		need(os.IsNotExist(statErr))
	} else {
		need(err == nil)
		body, err = os.ReadFile("/run/dittobench-control/response.json")
		need(err == nil)
		var result struct {
			Passed, Total   int
			Completed       bool
			ProcessTreeDead bool `json:"process_tree_dead"`
			Stdout, Stderr  string
		}
		need(json.Unmarshal(body, &result) == nil)
		passed := 2
		switch scenario {
		case "wrong", "exit", "hang", "fake-report", "fake-api", "oversized":
			passed = 1
		case "init-exit", "compile-fail", "compiler-private-read":
			passed = 0
		}
		need(result.Passed == passed && result.Total == 2 && result.Completed && result.ProcessTreeDead && result.Stdout == "" && result.Stderr == "")
	}
	fmt.Println("Go supervisor probe passed: " + scenario)
}
