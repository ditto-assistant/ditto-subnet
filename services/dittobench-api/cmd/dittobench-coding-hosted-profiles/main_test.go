package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestRunRejectsUnsafePathsAndRequestsBeforePayload(t *testing.T) {
	root := t.TempDir()
	write := func(name, body string) string {
		path := filepath.Join(root, name)
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	valid := `{"schema":"dittobench-coding-hosted-profile-request-v1","catalog_index":0,"shadow_only":true,"weight_eligible":false,"build":{"required":false,"command":{"id":"b","argv":["x"],"timeout_milliseconds":1}}}`
	missing := filepath.Join(root, "missing-payload.json")
	for name, args := range map[string][4]string{
		"relative-request":  {"request.json", missing, root, filepath.Join(root, "out-a")},
		"unclean-output":    {write("r1.json", valid), missing, root, root + "/./out-b"},
		"existing-output":   {write("r2.json", valid), missing, root, root},
		"unknown-field":     {write("r3.json", `{"schema":"dittobench-coding-hosted-profile-request-v1","extra":1}`), missing, root, filepath.Join(root, "out-c")},
		"trailing-document": {write("r4.json", valid+valid), missing, root, filepath.Join(root, "out-d")},
		"weight-eligible":   {write("r5.json", `{"schema":"dittobench-coding-hosted-profile-request-v1","catalog_index":0,"shadow_only":true,"weight_eligible":true}`), missing, root, filepath.Join(root, "out-e")},
		"missing-build":     {write("r6.json", `{"schema":"dittobench-coding-hosted-profile-request-v1","catalog_index":0,"shadow_only":true,"weight_eligible":false}`), missing, root, filepath.Join(root, "out-f")},
		"missing-payload":   {write("r7.json", valid), missing, root, filepath.Join(root, "out-g")},
	} {
		receipt, err := run(context.Background(), args[0], args[1], args[2], args[3])
		if err == nil || receipt != nil {
			t.Fatalf("%s accepted", name)
		}
	}
	for _, name := range []string{"out-a", "out-c", "out-d", "out-e", "out-f", "out-g"} {
		if _, err := os.Lstat(filepath.Join(root, name)); err == nil {
			t.Fatalf("%s created output", name)
		}
	}
}
