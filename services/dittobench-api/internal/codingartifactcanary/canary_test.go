package codingartifactcanary

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
)

type sourceFunc func(context.Context, codingartifacts.Capability) (io.ReadCloser, error)

func (function sourceFunc) Open(ctx context.Context, capability codingartifacts.Capability) (io.ReadCloser, error) {
	return function(ctx, capability)
}

func capabilityFixture(t *testing.T, root string, now time.Time, body []byte) (string, codingartifacts.WireCapability) {
	t.Helper()
	digest := sha256.Sum256(body)
	encodedDigest := hex.EncodeToString(digest[:])
	expires := now.Add(5 * time.Minute)
	value := codingartifacts.WireCapability{
		Schema:                "dittobench-coding-artifact-capability-v1",
		CodingContractVersion: 1, WeightEligible: false,
		TicketID:       "11111111-1111-4111-8111-111111111111",
		TicketDeadline: now.Add(time.Hour), DeliveryPhase: codingartifacts.PhaseAuthoring,
		ArtifactKind: codingartifacts.KindVisibleBundle,
		Audience:     codingartifacts.AudienceWorkspaceMaterializer,
		SHA256:       encodedDigest, SizeBytes: int64(len(body)), ExpiresAt: expires,
		URL: fmt.Sprintf(
			"https://coding-artifacts.example/coding-artifacts/v1/visible-bundle/sha256/%s?X-Amz-Signature=test&X-Amz-Date=%s&X-Amz-Expires=300",
			encodedDigest, now.UTC().Format("20060102T150405Z"),
		),
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "artifact-capability.json")
	if err := os.WriteFile(path, encoded, 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o400); err != nil {
		t.Fatal(err)
	}
	return path, value
}

func TestCanaryFetchesOneVisibleBundleAndWritesRedactedReceipt(t *testing.T) {
	now := time.Date(2026, 9, 2, 4, 0, 0, 0, time.UTC)
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	body := []byte("verified visible bundle")
	capabilityPath, wire := capabilityFixture(t, root, now, body)
	receiptPath := filepath.Join(root, "receipt.json")
	opened := 0
	source := sourceFunc(func(_ context.Context, capability codingartifacts.Capability) (io.ReadCloser, error) {
		opened++
		if capability.URL != wire.URL || capability.SHA256 != wire.SHA256 {
			t.Fatal("canary capability authority drifted")
		}
		return io.NopCloser(bytes.NewReader(body)), nil
	})
	current := now
	canary, err := New(Config{
		Artifacts: source, CapabilityPath: capabilityPath, ReceiptPath: receiptPath,
		Now: func() time.Time { value := current; current = current.Add(time.Second); return value },
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := canary.Run(t.Context()); err != nil {
		t.Fatal(err)
	}
	if opened != 1 {
		t.Fatalf("opened=%d", opened)
	}
	info, err := os.Stat(receiptPath)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("receipt info=%v err=%v", info, err)
	}
	receiptBody, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	var receipt map[string]any
	if err := json.Unmarshal(receiptBody, &receipt); err != nil {
		t.Fatal(err)
	}
	if receipt["status"] != "passed" || receipt["ticket_authority_used"] != true ||
		receipt["platform_contacted"] != false || receipt["s3_accessed"] != true ||
		receipt["candidate_executed"] != false ||
		receipt["artifact_sha256"] != wire.SHA256 {
		t.Fatalf("receipt=%v", receipt)
	}
	encodedReceipt := string(receiptBody)
	if strings.Contains(encodedReceipt, wire.URL) || strings.Contains(encodedReceipt, wire.TicketID) {
		t.Fatal("receipt exposed bearer URL or raw ticket identity")
	}
}

func TestCanaryRejectsWrongPhaseShortReadsAndFetchFailure(t *testing.T) {
	now := time.Date(2026, 9, 2, 4, 0, 0, 0, time.UTC)
	for name, mutate := range map[string]func(*codingartifacts.WireCapability){
		"grading": func(value *codingartifacts.WireCapability) {
			value.DeliveryPhase = codingartifacts.PhaseGrading
		},
		"grader": func(value *codingartifacts.WireCapability) {
			value.ArtifactKind = codingartifacts.KindGraderBundle
			value.Audience = codingartifacts.AudienceProtectedGrader
		},
	} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			if err := os.Chmod(root, 0o700); err != nil {
				t.Fatal(err)
			}
			capabilityPath, wire := capabilityFixture(t, root, now, []byte("bundle"))
			mutate(&wire)
			encoded, err := json.Marshal(wire)
			if err != nil || os.Chmod(capabilityPath, 0o600) != nil ||
				os.WriteFile(capabilityPath, encoded, 0o600) != nil || os.Chmod(capabilityPath, 0o400) != nil {
				t.Fatal("rewrite capability")
			}
			canary, err := New(Config{
				Artifacts: sourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
					return io.NopCloser(strings.NewReader("bundle")), nil
				}),
				CapabilityPath: capabilityPath, ReceiptPath: filepath.Join(root, "receipt.json"),
				Now: func() time.Time { return now },
			})
			if err != nil {
				t.Fatal(err)
			}
			if err := canary.Run(t.Context()); !errors.Is(err, ErrRun) {
				t.Fatalf("err=%v", err)
			}
		})
	}

	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	capabilityPath, _ := capabilityFixture(t, root, now, []byte("bundle"))
	for name, source := range map[string]ArtifactSource{
		"short": sourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
			return io.NopCloser(strings.NewReader("x")), nil
		}),
		"failed": sourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
			return nil, codingartifacts.ErrArtifactUnavailable
		}),
	} {
		t.Run(name, func(t *testing.T) {
			receiptPath := filepath.Join(root, name+"-receipt.json")
			canary, err := New(Config{
				Artifacts: source, CapabilityPath: capabilityPath, ReceiptPath: receiptPath,
				Now: func() time.Time { return now },
			})
			if err != nil {
				t.Fatal(err)
			}
			if err := canary.Run(t.Context()); !errors.Is(err, ErrRun) {
				t.Fatalf("err=%v", err)
			}
			if _, err := os.Stat(receiptPath); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("failed canary wrote a receipt")
			}
		})
	}
}

func TestCanaryRejectsUnsafeFilesAndKeepsDiagnosticsPrivate(t *testing.T) {
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	capabilityPath, _ := capabilityFixture(t, root, time.Now().UTC(), []byte("bundle"))
	config := Config{
		Artifacts: sourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
			return nil, errors.New("unused")
		}),
		CapabilityPath: capabilityPath, ReceiptPath: filepath.Join(root, "receipt.json"),
	}
	if strings.Contains(fmt.Sprintf("%#v", config), capabilityPath) {
		t.Fatal("config diagnostics exposed private paths")
	}
	if _, err := json.Marshal(config); !errors.Is(err, ErrInvalid) {
		t.Fatalf("marshal err=%v", err)
	}
	canary, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := json.Marshal(canary); !errors.Is(err, ErrInvalid) {
		t.Fatalf("canary marshal err=%v", err)
	}
	if err := os.Chmod(capabilityPath, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := New(config); !errors.Is(err, ErrInvalid) {
		t.Fatalf("unsafe capability err=%v", err)
	}
}
