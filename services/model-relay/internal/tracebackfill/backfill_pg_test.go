package tracebackfill

import (
	"context"
	"encoding/json"
	"io"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/klauspost/compress/zstd"

	"github.com/ditto-assistant/model-relay/internal/testutil"
	"github.com/ditto-assistant/model-relay/internal/traces"
)

// memSink collects objects in memory (the S3 wire path is covered in the
// traces package; this test is about the ledger walk and the deletes).
type memSink struct {
	mu      sync.Mutex
	objects map[string][]byte
	name    string
}

func (m *memSink) Name() string                 { return m.name }
func (m *memSink) Required() bool               { return true }
func (m *memSink) Ensure(context.Context) error { return nil }
func (m *memSink) Put(_ context.Context, key string, _ int64, _, _ string, open func() (io.ReadCloser, error)) error {
	rc, err := open()
	if err != nil {
		return err
	}
	defer rc.Close()
	b, err := io.ReadAll(rc)
	if err != nil {
		return err
	}
	m.mu.Lock()
	m.objects[key] = b
	m.mu.Unlock()
	return nil
}

func (m *memSink) records(t *testing.T) map[string][]traces.Record {
	t.Helper()
	m.mu.Lock()
	defer m.mu.Unlock()
	out := map[string][]traces.Record{}
	for key, body := range m.objects {
		dec, err := zstd.NewReader(strings.NewReader(string(body)))
		if err != nil {
			t.Fatal(err)
		}
		raw, err := io.ReadAll(dec)
		dec.Close()
		if err != nil {
			t.Fatal(err)
		}
		for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
			var r traces.Record
			if err := json.Unmarshal([]byte(line), &r); err != nil {
				t.Fatalf("bad record: %v", err)
			}
			out[key] = append(out[key], r)
		}
	}
	return out
}

func TestBackfillExportsLedgerRowsByDayAndDeletesOnlyWhatItShipped(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	agentID, grantOld, grantLive := uuid.New(), uuid.New(), uuid.New()
	hotkey := "5BackfillValidatorHotkeyAAAAAAAAAAAAAAAAAAAAAAAA"
	now := time.Now().UTC().Truncate(time.Microsecond)
	testutil.SeedSQL(t, pool,
		`INSERT INTO agents (agent_id, miner_hotkey, name, sha256)
		 VALUES ($1, 'miner-hotkey', 'backfill-agent', repeat('b', 64))`, agentID)
	testutil.SeedSQL(t, pool,
		`INSERT INTO validator_tickets (agent_id, validator_hotkey, slot_id, status, deadline, bench_version, attempt_count)
		 VALUES ($1, $2, 'slot-0', 'issued', $3, 9, 1)`, agentID, hotkey, now.Add(30*time.Minute))
	for _, g := range []struct {
		id      uuid.UUID
		status  string
		expires time.Time
		slot    string
	}{
		{grantOld, "revoked", now.Add(-10 * 24 * time.Hour), "slot-0"},
		{grantLive, "active", now.Add(20 * time.Minute), "slot-1"},
	} {
		testutil.SeedSQL(t, pool,
			`INSERT INTO inference_grants (
			    grant_id, agent_id, bench_version, validator_hotkey, slot_id, ticket_deadline, status,
			    generation, allowed_models, route_provider, route_profile, request_budget, token_budget,
			    expires_at, usage_accounting_version)
			 VALUES ($1, $2, 9, $3, $4, $5, $6, 1, '["openai/gpt-oss-20b"]'::jsonb, 'openrouter',
			         'openrouter-route-test-v1', 8192, 25000000, $5, 2)`,
			g.id, agentID, hotkey, g.slot, g.expires, g.status)
	}
	// Ten old rows across two UTC days under the expired grant, plus one
	// recent row under the live grant.
	oldStart := now.Add(-10 * 24 * time.Hour).Truncate(24 * time.Hour).Add(23*time.Hour + 50*time.Minute)
	for i := 0; i < 10; i++ {
		startedAt := oldStart.Add(time.Duration(i) * 2 * time.Minute) // crosses midnight after 5 rows
		testutil.SeedSQL(t, pool,
			`INSERT INTO inference_requests (grant_id, nonce, generation, status, model, reserved_tokens,
			    prompt_tokens, completion_tokens, cost_microusd, started_at, completed_at, upstream_provider,
			    timed_out, latency_ms, request_kind, upstream_attempts)
			 VALUES ($1, $2, 1, 'completed', 'openai/gpt-oss-20b', 64, 10, 5, 2100, $3, $4, 'deepinfra',
			         false, 800, 'chat', 1)`,
			grantOld, uuid.New(), startedAt, startedAt.Add(time.Second))
	}
	testutil.SeedSQL(t, pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, model, reserved_tokens,
		    prompt_tokens, completion_tokens, cost_microusd, started_at, request_kind)
		 VALUES ($1, $2, 1, 'started', 'openai/gpt-oss-20b', 64, 0, 0, 0, $3, 'chat')`,
		grantLive, uuid.New(), now.Add(-5*time.Minute))

	sink := &memSink{objects: map[string][]byte{}, name: "mem"}
	dir := t.TempDir()
	summary, err := Run(context.Background(), Options{
		Pool: pool, Sinks: []traces.Sink{sink}, SpoolDir: dir, BatchRows: 4, Until: now, Delete: true,
		Retain: 24 * time.Hour, DrainWait: 30 * time.Second,
	})
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	// Every row before `until` is exported (11), in 3 inference batches
	// (4+4+3) and 0 confirmation rows.
	if summary.RowsExported[laneInference] != 11 || summary.RowsExported[laneConfirmation] != 0 {
		t.Fatalf("exported: %+v", summary.RowsExported)
	}
	recs := sink.records(t)
	var days []string
	total := 0
	for key, rs := range recs {
		if !strings.HasPrefix(key, "ledger/v1/lane=inference/kind=chat/dt=") {
			t.Fatalf("key layout: %s", key)
		}
		days = append(days, key[strings.Index(key, "dt="):strings.Index(key, "/hour=")])
		total += len(rs)
		for _, r := range rs {
			if r.Event != traces.EventBackfill || r.Relay.Source != "postgres-backfill" || r.Grant == nil || r.Grant.AgentID != agentID.String() || r.Request.Body != nil {
				t.Fatalf("record: %+v", r)
			}
			day := key[strings.Index(key, "dt=")+3 : strings.Index(key, "/hour=")]
			if r.Outcome.StartedAt.UTC().Format("2006-01-02") != day {
				t.Fatalf("record %s filed under %s", r.Outcome.StartedAt, day)
			}
		}
	}
	if total != 11 {
		t.Fatalf("records in sink: %d", total)
	}
	if len(recs) < 2 {
		t.Fatalf("rows spanning midnight must land in separate day objects: %v", days)
	}
	// Deletes: the 10 old rows under the expired grant go; the live grant's
	// in-flight row stays although it was exported.
	if summary.RowsDeleted[laneInference] != 10 {
		t.Fatalf("deleted: %+v", summary.RowsDeleted)
	}
	var remaining int
	if err := pool.QueryRow(context.Background(), `SELECT count(*) FROM inference_requests`).Scan(&remaining); err != nil {
		t.Fatal(err)
	}
	if remaining != 1 {
		t.Fatalf("remaining rows: %d", remaining)
	}
	// Re-running is a no-op: the cursor marks both lanes done.
	again, err := Run(context.Background(), Options{Pool: pool, Sinks: []traces.Sink{sink}, SpoolDir: dir, Until: now})
	if err != nil {
		t.Fatal(err)
	}
	if again.Batches != 0 {
		t.Fatalf("second run should read nothing: %+v", again)
	}
	if _, err := loadCursor(filepath.Join(dir, "cursor.json")); err != nil {
		t.Fatal(err)
	}
}
