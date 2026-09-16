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
	err := run(context.Background(), []string{"observe-requested-config", "--image", "python=sha256:x"}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("missing profile and repository must be refused: %v", err)
	}
}
