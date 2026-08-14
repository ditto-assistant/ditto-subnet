package main

import (
	"os"
	"strings"
	"testing"
)

// The deploy tooling contract (build-relay-release.sh smoke check,
// ecosystem.config.js slot args, deploy-relay-release.sh canary):
//   - `model-relay --version` exits 0 and prints the ldflags-stamped commit;
//   - `--port N` selects the listen port, API_PORT is the fallback default.
func TestParseArgs(t *testing.T) {
	devNull, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if err != nil {
		t.Fatalf("open %s: %v", os.DevNull, err)
	}
	defer devNull.Close()

	cases := []struct {
		name    string
		args    []string
		want    cliOptions
		wantErr bool
	}{
		{name: "no args", args: nil, want: cliOptions{}},
		{name: "version double dash", args: []string{"--version"}, want: cliOptions{version: true}},
		{name: "version single dash", args: []string{"-version"}, want: cliOptions{version: true}},
		{name: "port double dash", args: []string{"--port", "8021"}, want: cliOptions{port: 8021}},
		{name: "port equals form", args: []string{"--port=8020"}, want: cliOptions{port: 8020}},
		{name: "port zero rejected by flag range", args: []string{"--port", "70000"}, wantErr: true},
		{name: "negative port", args: []string{"--port", "-1"}, wantErr: true},
		{name: "unknown flag", args: []string{"--bogus"}, wantErr: true},
		{name: "stray positional", args: []string{"extra"}, wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseArgs(tc.args, devNull)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("parseArgs(%v) = %+v, want error", tc.args, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("parseArgs(%v): %v", tc.args, err)
			}
			if got != tc.want {
				t.Fatalf("parseArgs(%v) = %+v, want %+v", tc.args, got, tc.want)
			}
		})
	}
}

func TestVersionLine(t *testing.T) {
	orig := buildCommit
	defer func() { buildCommit = orig }()

	buildCommit = ""
	if got := versionLine(); got != "model-relay unknown" {
		t.Fatalf("unstamped versionLine() = %q, want %q", got, "model-relay unknown")
	}

	sha := strings.Repeat("ab", 20)
	buildCommit = sha
	got := versionLine()
	if !strings.Contains(got, sha) {
		// build-relay-release.sh matches the smoke output on *substring*
		// containment of the 40-char source commit.
		t.Fatalf("stamped versionLine() = %q, does not contain %q", got, sha)
	}
}
