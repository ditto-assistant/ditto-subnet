package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertservice"
)

func TestDefaultEnvironmentIsDisabledWithoutSideEffects(t *testing.T) {
	var stderr bytes.Buffer
	code := run(t.Context(), nil, func(string) string { return "" }, os.Geteuid(), &stderr, codingcertservice.Run)
	if code != exitDisabled || strings.TrimSpace(stderr.String()) != codingcertservice.ErrDisabled.Error() {
		t.Fatalf("code=%d stderr=%q", code, stderr.String())
	}
	for _, path := range []string{codingcertservice.ControlSocketPath, codingcertservice.PrivateRoot} {
		if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("%s exists: %v", path, err)
		}
	}
}

func TestExitCodesPrintOnlyFixedMessages(t *testing.T) {
	const secret = "/run/credentials/secret-token-value"
	for _, test := range []struct {
		err  error
		code int
		want string
	}{
		{nil, exitOK, ""},
		{fmt.Errorf("%w: %s", codingcertservice.ErrAdmissionEnded, secret), exitOK, codingcertservice.ErrAdmissionEnded.Error()},
		{fmt.Errorf("%w: %s", codingcertservice.ErrDisabled, secret), exitDisabled, codingcertservice.ErrDisabled.Error()},
		{fmt.Errorf("%w: %s", codingcertservice.ErrPlacement, secret), exitPlacement, codingcertservice.ErrPlacement.Error()},
		{fmt.Errorf("%w: %s", codingcertservice.ErrConfig, secret), exitRefused, codingcertservice.ErrConfig.Error()},
		{errors.New(secret), exitRefused, codingcertservice.ErrUnavailable.Error()},
	} {
		var stderr bytes.Buffer
		serve := func(context.Context, func(string) string, int) error { return test.err }
		code := run(t.Context(), nil, func(string) string { return "" }, 1000, &stderr, serve)
		if code != test.code || strings.TrimSpace(stderr.String()) != test.want || strings.Contains(stderr.String(), "secret") {
			t.Fatalf("err=%v code=%d stderr=%q", test.err, code, stderr.String())
		}
	}
	var stderr bytes.Buffer
	called := false
	serve := func(context.Context, func(string) string, int) error { called = true; return nil }
	if code := run(t.Context(), []string{"--listen", "0.0.0.0:8000"}, nil, 1000, &stderr, serve); code != exitRefused || called {
		t.Fatalf("arguments accepted: code=%d called=%v", code, called)
	}
}
