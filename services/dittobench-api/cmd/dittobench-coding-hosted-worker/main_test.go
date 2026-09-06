package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestCommandIsExplicitAndNeverEchoesPrivateArguments(t *testing.T) {
	for _, args := range [][]string{
		nil, {"--config", "/private/secret-marker"}, {"--private-shadow-once"},
		{"--bad-secret-marker"}, {"--private-shadow-once", "--config", "/private/secret-marker", "extra"},
		{"--private-shadow-once", "--config", "/private/secret-marker"},
		{"--validate-only", "--config", "/private/secret-marker"},
		{"--validate-only", "--private-shadow-once", "--config", "/private/secret-marker"},
	} {
		var out, err bytes.Buffer
		if run(t.Context(), args, &out, &err) == 0 || out.Len() != 0 || strings.Contains(err.String(), "secret-marker") {
			t.Fatal("command activated or exposed private input")
		}
	}
}
