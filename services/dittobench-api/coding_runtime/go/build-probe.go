//go:build ignore

// Public container-only integration fixture. Never selected by host go test.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"go/token"
	"os"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggobuild"
	"github.com/ditto-assistant/dittobench-api/internal/codinggooracle"
)

func need(ok bool) {
	if !ok {
		panic("public Go build probe failed")
	}
}
func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	for _, directory := range []string{"/run/dittobench-grader", "/run/dittobench-control"} {
		need(os.MkdirAll(directory, 0700) == nil)
		need(os.Chmod(directory, 0700) == nil)
	}
	need(os.WriteFile("/run/dittobench-grader/secret", []byte("#define SECRET_VALUE 123\n"), 0400) == nil)
	request := codinggobuild.Request{ModulePath: "example.invalid/subject", PackageName: "subject", UID: 10001, GID: 10001, Functions: []string{"Add", "Change", "Fail", "Window", "Length"}, Files: []codinggobuild.File{
		{Path: "go.mod", Bytes: []byte("module example.invalid/subject\ngo 1.26.6\n")},
		{Path: "subject.go", Bytes: []byte(`package subject
import("os";"os/exec";"syscall";"errors";"fmt";"time")
//go:generate sh -c "touch /tmp/go-generate-ran"
func init(){if os.Getuid()==0{panic("root candidate")};if !errors.Is(exec.Command("/bin/true").Run(),syscall.EPERM){panic("unconfined init")};os.WriteFile("/tmp/go-candidate-init",[]byte("public marker"),0600);fmt.Println("ordinary init diagnostic")}
func Add(a,b int64)int64{return a+b}
func Change(values []string)[]string{values[0]="updated";return values}
func Fail(value string)(string,error){return "",fmt.Errorf("public error")}
func Window(a,b time.Time)bool{return a.Before(b)&&b.Location().String()=="synthetic"}
func Length(values []string)int{return len(values)}
`)},
	}}
	artifact, err := codinggobuild.Build(ctx, request)
	if err != nil {
		panic("fixed Go build failed")
	}
	defer artifact.Close()
	executable, err := artifact.OpenExecutable()
	need(err == nil)
	_, err = executable.WriteAt([]byte{0}, 0)
	need(errors.Is(err, syscall.EPERM))
	err = executable.Truncate(1)
	need(os.IsPermission(err))
	need(executable.Close() == nil)
	_, err = os.Stat("/tmp/go-candidate-init")
	need(os.IsNotExist(err))
	_, err = os.Stat("/tmp/go-generate-ran")
	need(os.IsNotExist(err))
	for _, path := range artifact.Exports {
		info, err := os.Stat(path)
		need(err == nil && info.Sys().(*syscall.Stat_t).Uid == 10001)
	}
	fmt.Println("candidate compiled without initialization or build hooks; exports owned by non-root compiler")
	wrong := artifact
	wrong.SHA256 = fmt.Sprintf("%064d", 0)
	if program, err := codinggobuild.Launch(ctx, wrong, 10001, 10001); err == nil {
		program.Close()
		panic("wrong executable digest accepted")
	}
	_, err = os.Stat("/tmp/go-candidate-init")
	need(os.IsNotExist(err))
	fmt.Println("sealed executable resists writes/truncation; mismatched digest rejected before entry")
	program, err := codinggobuild.Launch(ctx, artifact, 10001, 10001)
	if err != nil {
		panic("confined Go launch failed")
	}
	reader := bufio.NewReader(program.Output())
	call := func(function string, reference uint64, method string, arguments []any) map[string]any {
		id := "1234567890abcdef1234567890abcdef"
		request := map[string]any{"id": id, "arguments": arguments}
		if function != "" {
			request["function"] = function
		}
		if reference != 0 {
			request["reference"] = reference
			request["method"] = method
		}
		raw, err := json.Marshal(request)
		need(err == nil)
		raw = append(raw, '\n')
		_, err = program.Input().Write(raw)
		need(err == nil)
		need(program.Output().SetReadDeadline(time.Now().Add(5*time.Second)) == nil)
		line, err := reader.ReadBytes('\n')
		need(err == nil && len(line) <= 65536)
		var result map[string]any
		need(json.Unmarshal(line, &result) == nil && result["id"] == id && result["failed"] != true)
		return result
	}
	integer := func(text string) any { return map[string]any{"kind": "int64", "type": "int64", "text": text} }
	result := call("Add", 0, "", []any{integer("9223372036854775806"), integer("1")})
	need(result["values"].([]any)[0].(map[string]any)["text"] == "9223372036854775807")
	item := func(text string) any { return map[string]any{"kind": "string", "type": "string", "text": text} }
	list := map[string]any{"kind": "slice", "type": "[]string", "length": 2, "capacity": 2, "items": []any{item("original"), item("b")}}
	result = call("Change", 0, "", []any{list})
	need(result["arguments"].([]any)[0].(map[string]any)["items"].([]any)[0].(map[string]any)["text"] == "updated")
	result = call("Fail", 0, "", []any{item("input")})
	reference := uint64(result["values"].([]any)[1].(map[string]any)["reference"].(float64))
	result = call("", reference, "Error", []any{})
	need(result["values"].([]any)[0].(map[string]any)["text"] == "public error")
	instant := func(seconds, name string, offset int, location uint64, utc bool) any {
		return map[string]any{"kind": "time", "type": "time.Time", "instant": map[string]any{"seconds": seconds, "nanoseconds": 0, "location": location, "name": name, "offset": offset, "utc": utc}}
	}
	result = call("Window", 0, "", []any{instant("0", "UTC", 0, 0, true), instant("1", "synthetic", 3600, 1, false)})
	need(result["values"].([]any)[0].(map[string]any)["text"] == "true")
	need(program.Close() == nil)
	fmt.Println("confined Go API round trip passed: integer precision, slice effects, errors and time zones")
	oracleSource := []byte(`package subject;import("testing";"time")
func TestMath(t *testing.T){if Add(9223372036854775806,1)!=9223372036854775807{t.Fatal("private integer expectation")}}
func TestMutation(t *testing.T){values:=[]string{"original","b"};got:=Change(values);if values[0]!="updated"||got[0]!="updated"{t.Fatal("private slice expectation")}}
func TestError(t *testing.T){_,err:=Fail("input");if err==nil||err.Error()!="public error"{t.Fatal("private error expectation")}}
func TestTime(t *testing.T){a:=time.Date(2001,1,1,0,0,0,0,time.UTC);b:=a.Add(time.Second).In(time.FixedZone("synthetic",3600));if !Window(a,b){t.Fatal("private time expectation")}}
func TestNil(t *testing.T){if Length(nil)!=0{t.Fatal("private nil expectation")}}
`)
	suite, err := codinggooracle.Compile(oracleSource, codinggooracle.Config{PackagePath: request.ModulePath, CandidateSources: []codinggooracle.Source{{Name: "subject.go", Body: request.Files[1].Bytes}}, Functions: request.Functions, ExpectedTests: 5, Importer: artifact.Importer(token.NewFileSet())})
	need(err == nil)
	resultReport, err := codinggooracle.Run(ctx, suite, codinggooracle.ProcessFactory(artifact, suite.APISignatures(), 10001, 10001, 5*time.Second))
	need(err == nil && resultReport.Completed && resultReport.Passed == 5 && resultReport.Total == 5)
	fmt.Println("trusted Go assertions passed through actual confined processes")
	// Go's assembler must not read the protected oracle, even during compilation.
	bad := request
	bad.Functions = []string{"Secret"}
	bad.Files = []codinggobuild.File{request.Files[0], {Path: "subject.go", Bytes: []byte("package subject\nfunc Secret()int\n")}, {Path: "private.s", Bytes: []byte("#include \"textflag.h\"\n#include \"/run/dittobench-grader/secret\"\nTEXT ·Secret(SB),NOSPLIT,$0-8\nMOVQ $SECRET_VALUE, ret+0(FP)\nRET\n")}}
	if _, err := codinggobuild.Build(ctx, bad); err == nil {
		panic("compiler read private grader")
	}
	fmt.Println("non-root compiler private-file access rejected")
}
