package inference

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

func TestSettlementTraceDiagnosticDoesNotLeakErrorData(t *testing.T) {
	for _, tc := range []struct {
		name           string
		err            error
		kind, sqlstate string
	}{
		{"postgres", fmt.Errorf("wrapped: %w", &pgconn.PgError{Code: "40001", Message: "secret query", Detail: "private value", Where: "password=secret"}), "postgres_error", "40001"},
		{"invalid code", &pgconn.PgError{Code: "secret data"}, "postgres_error", ""},
		{"deadline", fmt.Errorf("wrapped: %w", context.DeadlineExceeded), "deadline_exceeded", ""},
		{"canceled", context.Canceled, "canceled", ""},
		{"other", errors.New("postgres://user:secret@private-host"), "internal_error", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := traceSettlementFailure(tc.err)
			if got.Kind != tc.kind || got.SQLState != tc.sqlstate {
				t.Fatalf("diagnostic=%+v", got)
			}
			raw, _ := json.Marshal(got)
			if strings.Contains(string(raw), "secret") || strings.Contains(string(raw), "private") {
				t.Fatalf("leaked error: %s", raw)
			}
		})
	}
	if traceSettlementFailure(nil) != nil {
		t.Fatal("successful settlement gained a failure")
	}
}
