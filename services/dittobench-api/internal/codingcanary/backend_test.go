package codingcanary

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codingphase"
)

type recordingHarnesses struct {
	acquired int
}

func (factory *recordingHarnesses) AcquireCanary(
	context.Context,
	codingharness.CanaryBinding,
) (codingphase.Harness, error) {
	factory.acquired++
	return nil, errors.New("canary harness should not be acquired")
}

func TestBackendRejectsPackMismatchBeforeAcquire(t *testing.T) {
	pack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	harnesses := &recordingHarnesses{}
	backend := &certifierBackend{pack: pack, harnesses: harnesses, now: time.Now}
	outcome, err := backend.Certify(t.Context(), Request{
		LeaseID:              "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
		CanaryManifestSHA256: strings.Repeat("d", 64),
	})
	if !errors.Is(err, ErrInvalid) {
		t.Fatalf("err=%v", err)
	}
	if harnesses.acquired != 0 {
		t.Fatalf("acquired=%d", harnesses.acquired)
	}
	if !outcome.CapabilitiesRevoked || !outcome.HarnessDestroyed {
		t.Fatalf("outcome=%+v", outcome)
	}
}
