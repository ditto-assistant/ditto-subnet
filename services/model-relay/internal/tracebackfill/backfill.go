// Package tracebackfill exports the historical Postgres inference ledgers
// (inference_requests and confirmation_inference_requests, joined to their
// grants) to the trace buckets through the same spool → sink pipeline the
// live capture uses, and can optionally delete the rows it has exported.
//
// The ledger rows are metadata only -- the relay never persisted bodies
// before the trace capture -- so a backfill record carries everything the
// row and its grant know (status, usage, timing, provider, attempts, route,
// validator, agent, bench version) with no request/response body. Records
// are partitioned by the row's own started_at (ledger/v1/lane=/kind=/dt=/hour=),
// one UTC day never shares an object with another.
//
// Resumable: a cursor file records the last (started_at, grant_id, nonce)
// exported per lane after every batch is *spooled*; the spool is durable on
// disk and shipped by the uploader, so a crash re-ships rather than re-reads.
// Deletion, when asked for, happens only after every spooled file has left
// the disk (every required sink confirmed it), only inside the exact key
// ranges this run exported, and only for rows older than the retention
// window whose grant has expired -- so nothing admission or settlement can
// still touch is ever removed.
package tracebackfill

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/traces"
)

// Options configures one backfill run.
type Options struct {
	Pool       *pgxpool.Pool
	Sinks      []traces.Sink
	SpoolDir   string        // REQUIRED: a directory private to the backfill (not the live spool)
	CursorPath string        // REQUIRED: progress file; "" -> <SpoolDir>/cursor.json
	Until      time.Time     // export rows started before this (default: now - 1h)
	BatchRows  int32         // rows per SELECT (default 5000)
	Lanes      []string      // subset of {inference, confirmation}; default both
	Delete     bool          // delete exported rows after upload
	Retain     time.Duration // rows younger than this are never deleted (default 168h; floor 24h)
	DrainWait  time.Duration // how long to wait for the spool to ship before deleting (default 30m)
	Logger     *slog.Logger
	Metrics    *traces.Metrics
	Now        func() time.Time
	// RotateBytes bounds each object (default 64 MiB uncompressed).
	RotateBytes int64
}

// keyRange is one exported batch's inclusive (first, last) ledger key.
type keyRange struct {
	FromStartedAt time.Time `json:"from_started_at"`
	FromGrantID   string    `json:"from_grant_id"`
	FromNonce     string    `json:"from_nonce"`
	ToStartedAt   time.Time `json:"to_started_at"`
	ToGrantID     string    `json:"to_grant_id"`
	ToNonce       string    `json:"to_nonce"`
	Rows          int64     `json:"rows"`
}

// cursor is the on-disk progress record.
type cursor struct {
	Lanes          map[string]*laneCursor `json:"lanes"`
	PendingDeletes map[string][]keyRange  `json:"pending_deletes,omitempty"`
}

type laneCursor struct {
	StartedAt time.Time `json:"started_at"`
	GrantID   string    `json:"grant_id"`
	Nonce     string    `json:"nonce"`
	Rows      int64     `json:"rows_exported"`
	Done      bool      `json:"done"`
}

// Summary is what a run did.
type Summary struct {
	RowsExported map[string]int64
	RowsDeleted  map[string]int64
	Batches      int
	Dropped      int64
}

const (
	laneInference    = traces.LaneInference
	laneConfirmation = traces.LaneConfirmation
)

// Run executes the backfill and returns a summary. It is safe to re-run.
func Run(ctx context.Context, o Options) (*Summary, error) {
	if o.Pool == nil {
		return nil, errors.New("backfill: pool is required")
	}
	if len(o.Sinks) == 0 {
		return nil, errors.New("backfill: at least one sink is required")
	}
	if o.SpoolDir == "" {
		return nil, errors.New("backfill: spool dir is required")
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.Metrics == nil {
		o.Metrics = traces.NopMetrics()
	}
	if o.BatchRows <= 0 {
		o.BatchRows = 5000
	}
	if o.Until.IsZero() {
		o.Until = o.Now().Add(-time.Hour)
	}
	if o.Retain < 24*time.Hour {
		o.Retain = 24 * time.Hour
	}
	if o.DrainWait <= 0 {
		o.DrainWait = 30 * time.Minute
	}
	if len(o.Lanes) == 0 {
		o.Lanes = []string{laneInference, laneConfirmation}
	}
	if o.CursorPath == "" {
		o.CursorPath = filepath.Join(o.SpoolDir, "cursor.json")
	}
	if err := os.MkdirAll(o.SpoolDir, 0o750); err != nil {
		return nil, err
	}
	cur, err := loadCursor(o.CursorPath)
	if err != nil {
		return nil, err
	}
	for _, sink := range o.Sinks {
		if err := sink.Ensure(ctx); err != nil {
			return nil, fmt.Errorf("backfill: sink %s: %w", sink.Name(), err)
		}
	}
	spool, err := traces.NewSpooler(traces.SpoolOptions{
		Dir:               filepath.Join(o.SpoolDir, "spool"),
		RotateBytes:       o.RotateBytes,
		RotateInterval:    time.Hour, // size and day boundaries drive rotation; age is a backstop
		MaxSpoolBytes:     64 << 30,
		QueueSize:         int(o.BatchRows) * 2,
		Instance:          "backfill",
		Source:            "postgres-backfill",
		RotateOnDayChange: true,
		Logger:            o.Logger,
		Metrics:           o.Metrics,
		Now:               o.Now,
	})
	if err != nil {
		return nil, err
	}
	uploader, err := traces.NewUploader(spool, traces.UploaderOptions{
		Sinks: o.Sinks, KeyPrefix: "ledger/v1", Logger: o.Logger, Metrics: o.Metrics, Now: o.Now,
		PollInterval: 2 * time.Second,
	})
	if err != nil {
		return nil, err
	}
	upCtx, cancelUp := context.WithCancel(ctx)
	uploader.Start(upCtx)
	summary := &Summary{RowsExported: map[string]int64{}, RowsDeleted: map[string]int64{}}
	q := postgres.New(o.Pool)

	var runErr error
	for _, lane := range o.Lanes {
		lc := cur.Lanes[lane]
		if lc == nil {
			lc = &laneCursor{}
			cur.Lanes[lane] = lc
		}
		if lc.Done {
			o.Logger.Info("backfill lane already complete", slog.String("lane", lane))
			continue
		}
		for {
			if ctx.Err() != nil {
				runErr = ctx.Err()
				break
			}
			var batch []*traces.Record
			var rng *keyRange
			var err error
			switch lane {
			case laneInference:
				batch, rng, err = o.readInferenceBatch(ctx, q, lc)
			case laneConfirmation:
				batch, rng, err = o.readConfirmationBatch(ctx, q, lc)
			default:
				err = fmt.Errorf("backfill: unknown lane %q", lane)
			}
			if err != nil {
				runErr = err
				break
			}
			if len(batch) == 0 {
				lc.Done = true
				if err := saveCursor(o.CursorPath, cur); err != nil {
					runErr = err
				}
				break
			}
			for _, rec := range batch {
				spool.Record(rec)
			}
			// Spooler.Record never blocks; a full queue drops. Wait for the
			// writer to absorb this batch before recording progress, so the
			// cursor never runs ahead of what is on disk.
			if err := spool.WaitIdle(ctx); err != nil {
				runErr = err
				break
			}
			if spool.Dropped() > summary.Dropped {
				runErr = fmt.Errorf("backfill: %d records dropped by the spool; lower --batch-rows or free disk", spool.Dropped()-summary.Dropped)
				summary.Dropped = spool.Dropped()
				break
			}
			lc.StartedAt, lc.GrantID, lc.Nonce = rng.ToStartedAt, rng.ToGrantID, rng.ToNonce
			lc.Rows += rng.Rows
			summary.RowsExported[lane] += rng.Rows
			summary.Batches++
			if o.Delete {
				if cur.PendingDeletes == nil {
					cur.PendingDeletes = map[string][]keyRange{}
				}
				cur.PendingDeletes[lane] = append(cur.PendingDeletes[lane], *rng)
			}
			if err := saveCursor(o.CursorPath, cur); err != nil {
				runErr = err
				break
			}
			if summary.Batches%20 == 0 {
				o.Logger.Info("backfill progress", slog.String("lane", lane), slog.Int64("rows", lc.Rows),
					slog.Time("through", lc.StartedAt))
			}
		}
		if runErr != nil {
			break
		}
	}

	// Flush the spool and let the uploader ship everything it can.
	closeCtx, cancelClose := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancelClose()
	if err := spool.Close(closeCtx); err != nil && runErr == nil {
		runErr = err
	}
	cancelUp()
	drainCtx, cancelDrain := context.WithTimeout(context.Background(), o.DrainWait)
	defer cancelDrain()
	uploader.Drain(drainCtx)

	if o.Delete && runErr == nil && len(cur.PendingDeletes) > 0 {
		if spool.Pending() > 0 {
			return summary, fmt.Errorf("backfill: %d spool files still not stored by every required sink; deletion skipped (re-run to retry)", spool.Pending())
		}
		retainBefore := o.Now().Add(-o.Retain)
		for lane, ranges := range cur.PendingDeletes {
			remaining := ranges[:0]
			for _, rng := range ranges {
				n, err := o.deleteRange(ctx, q, lane, rng, retainBefore)
				if err != nil {
					remaining = append(remaining, rng)
					runErr = errors.Join(runErr, err)
					continue
				}
				summary.RowsDeleted[lane] += n
			}
			if len(remaining) == 0 {
				delete(cur.PendingDeletes, lane)
			} else {
				cur.PendingDeletes[lane] = remaining
			}
		}
		if err := saveCursor(o.CursorPath, cur); err != nil {
			runErr = errors.Join(runErr, err)
		}
	}
	return summary, runErr
}

func (o Options) readInferenceBatch(ctx context.Context, q *postgres.Queries, lc *laneCursor) ([]*traces.Record, *keyRange, error) {
	rows, err := q.ListInferenceRequestsForBackfill(ctx, postgres.ListInferenceRequestsForBackfillParams{
		AfterStartedAt: tsFrom(lc.StartedAt), AfterGrantID: uuidFrom(lc.GrantID), AfterNonce: uuidFrom(lc.Nonce),
		Until: tsFrom(o.Until), BatchLimit: o.BatchRows,
	})
	if err != nil {
		return nil, nil, fmt.Errorf("backfill: list inference_requests: %w", err)
	}
	if len(rows) == 0 {
		return nil, nil, nil
	}
	out := make([]*traces.Record, 0, len(rows))
	for _, r := range rows {
		out = append(out, inferenceRecord(r))
	}
	first, last := rows[0], rows[len(rows)-1]
	return out, &keyRange{
		FromStartedAt: first.StartedAt.Time, FromGrantID: uuidString(first.GrantID), FromNonce: uuidString(first.Nonce),
		ToStartedAt: last.StartedAt.Time, ToGrantID: uuidString(last.GrantID), ToNonce: uuidString(last.Nonce),
		Rows: int64(len(rows)),
	}, nil
}

func (o Options) readConfirmationBatch(ctx context.Context, q *postgres.Queries, lc *laneCursor) ([]*traces.Record, *keyRange, error) {
	rows, err := q.ListConfirmationInferenceRequestsForBackfill(ctx, postgres.ListConfirmationInferenceRequestsForBackfillParams{
		AfterStartedAt: tsFrom(lc.StartedAt), AfterGrantID: uuidFrom(lc.GrantID), AfterNonce: uuidFrom(lc.Nonce),
		Until: tsFrom(o.Until), BatchLimit: o.BatchRows,
	})
	if err != nil {
		return nil, nil, fmt.Errorf("backfill: list confirmation_inference_requests: %w", err)
	}
	if len(rows) == 0 {
		return nil, nil, nil
	}
	out := make([]*traces.Record, 0, len(rows))
	for _, r := range rows {
		out = append(out, confirmationRecord(r))
	}
	first, last := rows[0], rows[len(rows)-1]
	return out, &keyRange{
		FromStartedAt: first.StartedAt.Time, FromGrantID: uuidString(first.GrantID), FromNonce: uuidString(first.Nonce),
		ToStartedAt: last.StartedAt.Time, ToGrantID: uuidString(last.GrantID), ToNonce: uuidString(last.Nonce),
		Rows: int64(len(rows)),
	}, nil
}

func (o Options) deleteRange(ctx context.Context, q *postgres.Queries, lane string, rng keyRange, retainBefore time.Time) (int64, error) {
	switch lane {
	case laneInference:
		return q.DeleteBackfilledInferenceRequests(ctx, postgres.DeleteBackfilledInferenceRequestsParams{
			FromStartedAt: tsFrom(rng.FromStartedAt), FromGrantID: uuidFrom(rng.FromGrantID), FromNonce: uuidFrom(rng.FromNonce),
			ToStartedAt: tsFrom(rng.ToStartedAt), ToGrantID: uuidFrom(rng.ToGrantID), ToNonce: uuidFrom(rng.ToNonce),
			RetainBefore: tsFrom(retainBefore),
		})
	case laneConfirmation:
		return q.DeleteBackfilledConfirmationInferenceRequests(ctx, postgres.DeleteBackfilledConfirmationInferenceRequestsParams{
			FromStartedAt: tsFrom(rng.FromStartedAt), FromGrantID: uuidFrom(rng.FromGrantID), FromNonce: uuidFrom(rng.FromNonce),
			ToStartedAt: tsFrom(rng.ToStartedAt), ToGrantID: uuidFrom(rng.ToGrantID), ToNonce: uuidFrom(rng.ToNonce),
			RetainBefore: tsFrom(retainBefore),
		})
	}
	return 0, fmt.Errorf("backfill: unknown lane %q", lane)
}

func inferenceRecord(r postgres.ListInferenceRequestsForBackfillRow) *traces.Record {
	kind := r.RequestKind
	started := traces.TimePtr(r.StartedAt.Time)
	var completed *time.Time
	if r.CompletedAt.Valid {
		completed = traces.TimePtr(r.CompletedAt.Time)
	}
	var latency int64
	if r.LatencyMs.Valid {
		latency = int64(r.LatencyMs.Int32)
	}
	return &traces.Record{
		Event: traces.EventBackfill,
		Request: traces.Request{
			Lane: traces.LaneInference, Kind: kind, GrantID: uuidString(r.GrantID), Nonce: uuidString(r.Nonce),
			Generation: int64(r.Generation), ReceivedAt: started,
		},
		Grant: &traces.Grant{
			AgentID: uuidString(r.AgentID), BenchVersion: r.BenchVersion, ValidatorHotkey: r.ValidatorHotkey,
			SlotID: r.SlotID, TicketDeadline: tsPtr(r.TicketDeadline), Status: r.GrantStatus, Generation: r.GrantGeneration,
			Model: r.Model, AllowedModels: traces.RawJSON(r.AllowedModels), RouteProvider: r.RouteProvider.String,
			RouteProfile: r.RouteProfile.String, RouteQuant: r.RouteQuantization.String, ExpiresAt: tsPtr(r.GrantExpiresAt),
		},
		Admission: &traces.Admission{ReservedTokens: r.ReservedTokens, MaxChargeableTokens: r.MaxChargeableTokens, AdmittedAt: started},
		Upstream: &traces.Upstream{
			Provider: r.UpstreamProvider.String, Model: r.Model, Attempts: int(r.UpstreamAttempts),
			OpenRouterAttempts: int(r.OpenrouterAttempts), FallbackPhase: int(r.FallbackPhase), TimedOut: r.TimedOut,
			TerminalErrorCode: r.TerminalErrorCode.String, StartedAt: started, FinishedAt: completed, LatencyMs: latency,
		},
		Usage: &traces.Usage{
			PromptTokens: r.PromptTokens, CompletionTokens: r.CompletionTokens, CostMicrousd: r.CostMicrousd,
			UsageAvailable: r.Status == "completed",
		},
		Outcome: &traces.Outcome{Status: r.Status, StartedAt: started, CompletedAt: completed},
	}
}

func confirmationRecord(r postgres.ListConfirmationInferenceRequestsForBackfillRow) *traces.Record {
	kind := traces.KindChat
	if r.Lane == "embedding" {
		kind = traces.KindEmbedding
	}
	started := traces.TimePtr(r.StartedAt.Time)
	var completed *time.Time
	if r.CompletedAt.Valid {
		completed = traces.TimePtr(r.CompletedAt.Time)
	}
	var latency int64
	if completed != nil {
		latency = completed.Sub(*started).Milliseconds()
	}
	return &traces.Record{
		Event: traces.EventBackfill,
		Request: traces.Request{
			Lane: traces.LaneConfirmation, Kind: kind, GrantID: uuidString(r.GrantID), Nonce: uuidString(r.Nonce),
			Generation: int64(r.Generation), ReceivedAt: started,
		},
		Grant: &traces.Grant{
			ValidatorHotkey: r.ValidatorHotkey, Status: r.GrantStatus, Generation: r.GrantGeneration, Model: r.GrantModel,
			RouteProvider: r.RouteProvider, ExpiresAt: tsPtr(r.GrantExpiresAt), TicketID: uuidString(r.TicketID),
			BundleID: uuidString(r.BundleID), Lane: r.Lane, Provider: r.Provider, ReceiptProvider: r.ReceiptProvider,
			ProfileRevision: r.ProfileRevision,
		},
		Admission: &traces.Admission{ReservedTokens: r.ReservedTokens, MaxChargeableTokens: r.MaxChargeableTokens, AdmittedAt: started},
		Upstream: &traces.Upstream{
			Provider: r.UpstreamProvider.String, Model: r.Model, StartedAt: started, FinishedAt: completed, LatencyMs: latency,
		},
		Usage: &traces.Usage{
			PromptTokens: r.PromptTokens, CompletionTokens: r.CompletionTokens, CostMicrousd: r.CostMicrousd,
			UsageAvailable: r.Status == "completed",
		},
		Outcome: &traces.Outcome{Status: r.Status, StartedAt: started, CompletedAt: completed},
	}
}

func loadCursor(path string) (*cursor, error) {
	c := &cursor{Lanes: map[string]*laneCursor{}}
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return c, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(b, c); err != nil {
		return nil, fmt.Errorf("backfill: cursor %s is corrupt: %w", path, err)
	}
	if c.Lanes == nil {
		c.Lanes = map[string]*laneCursor{}
	}
	return c, nil
}

func saveCursor(path string, c *cursor) error {
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o640); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func tsFrom(t time.Time) pgtype.Timestamptz {
	if t.IsZero() {
		// Before every real row: the ledger began in 2026.
		return pgtype.Timestamptz{Time: time.Unix(0, 0).UTC(), Valid: true}
	}
	return pgtype.Timestamptz{Time: t, Valid: true}
}

func tsPtr(t pgtype.Timestamptz) *time.Time {
	if !t.Valid {
		return nil
	}
	return traces.TimePtr(t.Time)
}

func uuidFrom(s string) pgtype.UUID {
	if s == "" {
		return pgtype.UUID{Bytes: uuid.Nil, Valid: true}
	}
	u, err := uuid.Parse(s)
	if err != nil {
		return pgtype.UUID{Bytes: uuid.Nil, Valid: true}
	}
	return pgtype.UUID{Bytes: u, Valid: true}
}

func uuidString(u pgtype.UUID) string {
	if !u.Valid {
		return ""
	}
	return uuid.UUID(u.Bytes).String()
}
