//go:build linux

package codinggobuild

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestCompilerEnvironmentHasNoAmbientHooksOrNetwork(t *testing.T) {
	environment := Environment("/private-build")
	values := map[string]string{}
	for _, entry := range environment {
		key, value, _ := strings.Cut(entry, "=")
		values[key] = value
	}
	for key, want := range map[string]string{"CGO_ENABLED": "0", "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off", "GOVCS": "*:off", "GOWORK": "off", "GOENV": "off", "GOFLAGS": "", "GOOS": "linux", "GOARCH": "amd64"} {
		if values[key] != want {
			t.Fatal("unsafe compiler option", key)
		}
	}
	for _, key := range []string{"HOME", "LD_PRELOAD", "GOPACKAGESDRIVER", "GIT_CONFIG", "GITHUB_TOKEN"} {
		if _, ok := values[key]; ok {
			t.Fatal("ambient setting forwarded")
		}
	}
}
func TestBuildInputsRefuseTraversalOverridesAndWrongModule(t *testing.T) {
	valid := Request{ModulePath: "example.invalid/subject", PackageName: "subject", Files: []File{{Path: "go.mod", Bytes: []byte("module example.invalid/subject\ngo 1.26.6\n")}}, Functions: []string{"Add"}, UID: 10001, GID: 10001}
	if err := validate(valid); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"../outside", "/outside", "a/../outside", "hidden_test.go", adapterFile, runnerDirectory + "/main.go"} {
		request := valid
		request.Files = append(append([]File(nil), valid.Files...), File{Path: name})
		if validate(request) == nil {
			t.Fatal("unsafe path accepted")
		}
	}
	wrong := valid
	wrong.ModulePath = "other.invalid/subject"
	if validate(wrong) == nil {
		t.Fatal("wrong module accepted")
	}
	if body, err := json.Marshal(valid); err == nil || len(body) != 0 {
		t.Fatal("private build request serialized")
	}
}
