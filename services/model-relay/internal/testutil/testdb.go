// Package testutil provisions real-Postgres test databases for the relay's
// *_pg_test.go suite, mirroring ditto-assistant/backend's pkg/testutil
// design adapted to the monorepo's ambient test container.
//
// Resolution order for the admin DSN:
//  1. TEST_POSTGRES_URI (same escape hatch as the backend harness; CI service
//     containers set this), else
//  2. the monorepo's ambient platform test Postgres
//     (ditto-platform-test-postgres, localhost:15433, ditto_test/ditto_test)
//     — started by the apps/platform pgharness, `make stack-up`, or
//     services/model-relay/scripts/gen-schema.sh.
//
// When neither is reachable every pg test SKIPS — unless
// TEST_POSTGRES_REQUIRED is set (CI sets it wherever a harness database is
// provisioned), in which case an unreachable database FAILS the test: a CI
// context that silently skipped the entire money-critical *_pg_test.go suite
// would merge green with zero DB coverage.
//
// Each test gets a FRESH database named model_relay_test_<pid>_<nano>
// (dropped in t.Cleanup — the 15433 container is shared with the platform
// suite, so leaving databases behind would be rude), loaded with
// db/schema.sql: the pg_dump of the real Alembic chain produced by
// scripts/gen-schema.sh. Tests therefore run against the real schema,
// including CHECK constraints, enums, triggers, and indexes.
package testutil

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ditto-assistant/model-relay/internal/postgres"
)

const defaultAdminURI = "postgres://ditto_test:ditto_test@localhost:15433/postgres?sslmode=disable"

var (
	adminOnce sync.Once
	adminURI  string
	adminErr  error

	schemaOnce sync.Once
	schemaSQL  string
	schemaErr  error
)

func ensureAdminURI() (string, error) {
	adminOnce.Do(func() {
		uri := os.Getenv("TEST_POSTGRES_URI")
		if uri == "" {
			uri = defaultAdminURI
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		pool, err := pgxpool.New(ctx, uri)
		if err != nil {
			adminErr = fmt.Errorf("parse admin uri: %w", err)
			return
		}
		defer pool.Close()
		if err := pool.Ping(ctx); err != nil {
			adminErr = fmt.Errorf("admin postgres unreachable at %s: %w", uri, err)
			return
		}
		adminURI = uri
	})
	return adminURI, adminErr
}

// loadSchemaSQL reads db/schema.sql relative to this source file.
func loadSchemaSQL() (string, error) {
	schemaOnce.Do(func() {
		_, self, _, ok := runtime.Caller(0)
		if !ok {
			schemaErr = fmt.Errorf("cannot locate testutil source file")
			return
		}
		path := filepath.Join(filepath.Dir(self), "..", "..", "db", "schema.sql")
		b, err := os.ReadFile(path)
		if err != nil {
			schemaErr = fmt.Errorf("read schema.sql (run scripts/gen-schema.sh?): %w", err)
			return
		}
		schemaSQL = string(b)
	})
	return schemaSQL, schemaErr
}

// skipOrFail skips the test when Postgres is genuinely optional (local runs
// without the ambient container) and FAILS it when the environment declared
// that a harness database must exist (TEST_POSTGRES_REQUIRED, set by CI jobs
// that provision one). Skipping in that context would silently drop the
// whole pg suite from a green pipeline.
func skipOrFail(t *testing.T, err error) {
	t.Helper()
	required := os.Getenv("TEST_POSTGRES_REQUIRED")
	if required != "" && required != "0" && required != "false" {
		t.Fatalf("TEST_POSTGRES_REQUIRED is set but the Postgres test database is unavailable: %v", err)
	}
	t.Skipf("Postgres test database unavailable: %v", err)
}

func withDatabase(adminURI, dbName string) (string, error) {
	parsed, err := url.Parse(adminURI)
	if err != nil {
		return "", fmt.Errorf("parse postgres url: %w", err)
	}
	parsed.Path = "/" + dbName
	query := parsed.Query()
	query.Set("search_path", "public")
	parsed.RawQuery = query.Encode()
	return parsed.String(), nil
}

// NewTestPGPool provisions a fresh migrated database for this test and
// returns a pool connected to it. The test is skipped (not failed) when
// Postgres is unavailable; the database is dropped in t.Cleanup.
func NewTestPGPool(t *testing.T) *pgxpool.Pool {
	t.Helper()

	admin, err := ensureAdminURI()
	if err != nil {
		skipOrFail(t, err)
	}
	schema, err := loadSchemaSQL()
	if err != nil {
		t.Fatalf("testutil: %v", err)
	}

	ctx := t.Context()
	adminPool, err := pgxpool.New(ctx, admin)
	if err != nil {
		skipOrFail(t, err)
	}
	defer adminPool.Close()

	dbName := fmt.Sprintf("model_relay_test_%d_%d", os.Getpid(), time.Now().UnixNano())
	if _, err := adminPool.Exec(ctx, "CREATE DATABASE "+dbName); err != nil {
		t.Fatalf("testutil: create test database %s: %v", dbName, err)
	}
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer cancel()
		cleanupPool, err := pgxpool.New(cleanupCtx, admin)
		if err != nil {
			t.Errorf("testutil: cleanup connect: %v", err)
			return
		}
		defer cleanupPool.Close()
		_, _ = cleanupPool.Exec(cleanupCtx,
			"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()", dbName)
		if _, err := cleanupPool.Exec(cleanupCtx, "DROP DATABASE IF EXISTS "+dbName); err != nil {
			t.Errorf("testutil: drop test database %s: %v", dbName, err)
		}
	})

	dbURI, err := withDatabase(admin, dbName)
	if err != nil {
		t.Fatalf("testutil: %v", err)
	}
	// Apply the generated schema mirror on a DEDICATED connection, closed
	// before the test pool opens: the dump runs
	// set_config('search_path', '', false), which would otherwise stick to a
	// pooled session and break unqualified names for whoever gets that
	// connection next. Exec with no arguments uses the simple query
	// protocol, so the whole multi-statement dump runs as one script.
	applyConn, err := pgx.Connect(ctx, dbURI)
	if err != nil {
		t.Fatalf("testutil: connect test database: %v", err)
	}
	if _, err := applyConn.Exec(ctx, schema); err != nil {
		_ = applyConn.Close(ctx)
		t.Fatalf("testutil: apply db/schema.sql: %v", err)
	}
	if err := applyConn.Close(ctx); err != nil {
		t.Fatalf("testutil: close schema connection: %v", err)
	}

	pool, err := pgxpool.New(ctx, dbURI)
	if err != nil {
		t.Fatalf("testutil: connect test database: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

// NewTestPGQueries returns generated Queries bound to a fresh migrated test
// database (see NewTestPGPool).
func NewTestPGQueries(t *testing.T) (*postgres.Queries, *pgxpool.Pool) {
	t.Helper()
	pool := NewTestPGPool(t)
	return postgres.New(pool), pool
}

// SeedSQL executes a fixture statement and fails the test on error.
func SeedSQL(t *testing.T, pool *pgxpool.Pool, query string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), query, args...); err != nil {
		t.Fatalf("testutil: seed %q: %v", query, err)
	}
}
