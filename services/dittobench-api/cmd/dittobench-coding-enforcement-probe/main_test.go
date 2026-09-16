package main

import (
	"bytes"
	"context"
	"strings"
	"testing"
)

func TestTheCommandCannotAssembleARecord(t *testing.T) {
	for _, subcommand := range []string{"assemble", "collect", "record"} {
		err := run(context.Background(), []string{subcommand}, &bytes.Buffer{})
		if err == nil || !strings.Contains(err.Error(), "unknown subcommand") {
			t.Fatalf("%s must not exist: %v", subcommand, err)
		}
	}
}

func TestObserveRequestedConfigRequiresApprovedInputs(t *testing.T) {
	err := run(context.Background(), []string{"observe-requested-config", "--language", "python"}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("missing profile, image set and repository must be refused: %v", err)
	}
	// A caller can no longer name an image digest; only the pinned set can.
	err = run(context.Background(), []string{"observe-requested-config", "--image", "python=sha256:x"}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "flag provided but not defined") {
		t.Fatalf("--image must not exist: %v", err)
	}
}
