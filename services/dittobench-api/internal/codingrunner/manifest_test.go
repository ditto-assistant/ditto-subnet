package codingrunner

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"
)

func TestRunnerCanonicalJSONMatchesCodingUnicodePolicy(t *testing.T) {
	body, err := canonicalStruct(map[string]any{"z": "<tag> & \u2028 \u2029", "a": 1})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(body, []byte("{\"a\":1,\"z\":\"<tag> & \\u2028 \\u2029\"}\n")) {
		t.Fatalf("canonical runner JSON=%q", body)
	}
}

func TestManifestFailsClosedOnMutableAuthority(t *testing.T) {
	bundle := fixtureBundle(t, false)
	base := fixtureManifest(t, bundle)
	tests := map[string]func(*Manifest){
		"wrong contract":     func(value *Manifest) { value.CodingContractVersion = 2 },
		"expired":            func(value *Manifest) { value.Deadline = time.Now().Add(-time.Second) },
		"unbounded lifetime": func(value *Manifest) { value.Deadline = time.Now().Add(3 * time.Hour) },
		"unsorted paths":     func(value *Manifest) { value.EditablePaths = []string{"z.py", "a.py"} },
		"overlapping paths": func(value *Manifest) {
			value.CreatablePaths = []string{"src/parser.py"}
		},
		"unsafe path":       func(value *Manifest) { value.EditablePaths = []string{"../escape.py"} },
		"control path":      func(value *Manifest) { value.EditablePaths = []string{"src/bad\nname.py"} },
		"general shell":     func(value *Manifest) { value.TestCommands[0].Argv = []string{"sh", "-c", "pytest"} },
		"absolute command":  func(value *Manifest) { value.TestCommands[0].Argv = []string{"/usr/bin/python", "-m", "pytest"} },
		"duplicate command": func(value *Manifest) { value.TestCommands = append(value.TestCommands, value.TestCommands[0]) },
		"shared command ID": func(value *Manifest) { value.BuildCommands[0].ID = "visible-tests" },
		"oversized limit":   func(value *Manifest) { value.Limits.MaxPatchBytes = hardMaxPatchBytes + 1 },
		"undersized replay cache": func(value *Manifest) {
			value.Limits.MaxReplayCacheBytes = int64(value.Limits.MaxToolCalls)*int64(value.Limits.MaxResponseBytes) - 1
		},
		"undersized transcript": func(value *Manifest) {
			perEvent := 2*MaxToolRequestBytes + int64(value.Limits.MaxResponseBytes) + 8192
			value.Limits.MaxTranscriptBytes = int64(value.Limits.MaxToolCalls)*perEvent - 1
		},
		"path policy count": func(value *Manifest) { value.Limits.MaxEntries = 2 },
		"command policy count": func(value *Manifest) {
			value.Limits.MaxToolCalls = 1
			value.Limits.MaxReplayCacheBytes = int64(value.Limits.MaxResponseBytes)
			value.Limits.MaxTranscriptBytes = 2*MaxToolRequestBytes + int64(value.Limits.MaxResponseBytes) + 8192
		},
		"read above response": func(value *Manifest) {
			value.Limits.MaxReadBytes = 4096
			value.Limits.MaxResponseBytes = 4096
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			manifest := fixtureManifest(t, bundle)
			mutate(&manifest)
			if _, err := NewSession(context.Background(), manifest, bytes.NewReader(bundle), nil); err == nil {
				t.Fatal("invalid runner manifest was accepted")
			}
		})
	}

	creatableExists := base
	creatableExists.CreatablePaths = []string{"src/parser.py"}
	creatableExists.EditablePaths = []string{}
	if _, err := NewSession(context.Background(), creatableExists, bytes.NewReader(bundle), nil); err == nil || !strings.Contains(err.Error(), "creatable path") {
		t.Fatalf("existing creatable path error=%v", err)
	}
	absentEditable := fixtureManifest(t, bundle)
	absentEditable.EditablePaths = []string{"missing.py"}
	if _, err := NewSession(context.Background(), absentEditable, bytes.NewReader(bundle), nil); err == nil || !strings.Contains(err.Error(), "absent") {
		t.Fatalf("absent editable path error=%v", err)
	}
}

func TestToolResultLimitProducesOneReplayableFailure(t *testing.T) {
	bundle := fixtureBundle(t, false)
	manifest := fixtureManifest(t, bundle)
	manifest.Limits.MaxReadBytes = 1024
	manifest.Limits.MaxResponseBytes = 4096
	session, err := NewSession(context.Background(), manifest, bytes.NewReader(bundle), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	response, err := session.Invoke(t.Context(), toolRequest("large-result", "repo.list_tree", map[string]any{"path": ".", "depth": 8}))
	if err != nil {
		t.Fatal(err)
	}
	// The small fixture may still fit. Force a deterministic oversized diff by
	// applying a large but manifest-valid edit, then requesting the full diff.
	if response.OK {
		invokeOK(t, session, toolRequest("large-edit", "repo.apply_patch", map[string]any{
			"path": "src/parser.py", "expected_sha256": session.base["src/parser.py"].sha256,
			"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": strings.Repeat("x", 5000)}},
		}))
		request := toolRequest("large-diff", "git.diff", map[string]any{})
		response, err = session.Invoke(t.Context(), request)
		if err != nil {
			t.Fatal(err)
		}
		replay, replayErr := session.Invoke(t.Context(), request)
		if replayErr != nil || replay.Sequence != response.Sequence || replay.EventSHA256 != response.EventSHA256 {
			t.Fatalf("result-limit replay diverged: %#v %#v %v", response, replay, replayErr)
		}
	}
	if response.OK || response.Error == nil || response.Error.Code != "result_limit" {
		t.Fatalf("oversized result was not recorded: %#v", response)
	}
}
