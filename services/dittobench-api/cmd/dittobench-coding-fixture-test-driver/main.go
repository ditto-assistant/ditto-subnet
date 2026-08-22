// Binary dittobench-coding-fixture-test-driver is a public certification-only
// trusted driver. Production grader images replace it with repository-specific
// drivers that isolate candidate code and report real test events.
package main

import (
	"encoding/json"
	"flag"
	"io"
	"os"
)

type report struct {
	Schema    string `json:"schema"`
	Nonce     string `json:"nonce"`
	Passed    uint64 `json:"passed"`
	Total     uint64 `json:"total"`
	Completed bool   `json:"completed"`
}

func main() {
	flags := flag.NewFlagSet("dittobench-test-driver", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	path := flags.String("dittobench-report", "", "trusted report path")
	nonce := flags.String("dittobench-nonce", "", "trusted report nonce")
	expected := flags.Uint64("dittobench-expected", 0, "expected test count")
	_ = flags.Uint64("dittobench-candidate-uid", 0, "candidate uid")
	_ = flags.Uint64("dittobench-candidate-gid", 0, "candidate gid")
	if err := flags.Parse(os.Args[1:]); err != nil || *path == "" || *nonce == "" || *expected == 0 {
		os.Exit(64)
	}
	handle, err := os.OpenFile(*path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		os.Exit(74)
	}
	encodeErr := json.NewEncoder(handle).Encode(report{
		Schema: "dittobench-coding-trusted-test-report-v1", Nonce: *nonce,
		Passed: *expected, Total: *expected, Completed: true,
	})
	syncErr := handle.Sync()
	closeErr := handle.Close()
	if encodeErr != nil || syncErr != nil || closeErr != nil {
		os.Exit(74)
	}
}
