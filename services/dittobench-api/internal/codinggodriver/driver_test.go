//go:build linux

package codinggodriver

import (
	"encoding/json"
	"strings"
	"testing"
)

func validArguments() []string {
	return []string{"--group", "hidden", "--suite", "hidden_test.go", "--package-path", "example.invalid/subject", "--function", "Add", "--candidate-timeout-ms", "1000", "--dittobench-report", reportPath, "--dittobench-nonce", strings.Repeat("a", 48), "--dittobench-expected", "1", "--dittobench-candidate-uid", "10001", "--dittobench-candidate-gid", "10001"}
}
func TestParseAuthorityAndPrivateOptions(t *testing.T) {
	options, err := Parse(validArguments())
	if err != nil || options.Expected != 1 || options.UID != 10001 {
		t.Fatal(err)
	}
	if body, err := json.Marshal(options); err == nil || len(body) != 0 {
		t.Fatal("private options serialized")
	}
	for _, pair := range [][2]string{{"suite", "../hidden_test.go"}, {"dittobench-report", "/tmp/report"}, {"dittobench-candidate-uid", "0"}, {"dittobench-expected", "1025"}, {"candidate-timeout-ms", "1e3"}, {"function", "init"}, {"function", "_"}, {"function", "bad-name"}, {"function", "main"}} {
		args := validArguments()
		for i := range args {
			if args[i] == "--"+pair[0] {
				args[i+1] = pair[1]
			}
		}
		if _, err := Parse(args); err == nil {
			t.Fatal("invalid authority accepted")
		}
	}
	if _, err := Parse(append(validArguments(), "--dittobench-nonce", strings.Repeat("b", 48))); err == nil {
		t.Fatal("duplicate supervisor authority accepted")
	}
	if _, err := Parse(append(validArguments(), "--function", "Add")); err == nil {
		t.Fatal("duplicate API function accepted")
	}
}
func TestCountsOnlyGoTestFunctions(t *testing.T) {
	count, err := testCount([]byte(`package subject;func helper(){};func Test(){};func TestOne(){};func Testlower(){};func Test2(){}`))
	if err != nil || count != 3 {
		t.Fatal("wrong Go test discovery")
	}
}
