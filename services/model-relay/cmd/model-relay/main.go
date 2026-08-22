// Command model-relay is the Go replacement for the Python platform's
// DITTO_ROLE=relay process: the SN118 inference plane plus narrow upload
// pricing/admission routes. It reads the exact environment the Python relay
// reads (apps/platform .env + .env.deploy on the host), so a host cutover
// needs no env changes; platform-only variables are tolerated and ignored.
//
// The relay owns NO migrations: apps/platform's Alembic chain owns the
// schema, and this process only reads/writes existing tables.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/ditto-assistant/model-relay/internal/chain"
	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/inference"
	"github.com/ditto-assistant/model-relay/internal/metrics"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/pprofserver"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/server"
	"github.com/ditto-assistant/model-relay/internal/tracebackfill"
	"github.com/ditto-assistant/model-relay/internal/traces"
	"github.com/ditto-assistant/model-relay/internal/upload"
)

// buildCommit is stamped by the release build via
// -ldflags "-X main.buildCommit=<40-char sha>". It backs `--version` only;
// the /health commit field is sourced from DITTO_BUILD_COMMIT at runtime
// (the deploy scripts export both from the same source-commit marker).
var buildCommit string

const legacyRecoveryResponseHeaderTimeout = 30 * time.Second

// cliOptions is the parsed command line. The relay accepts exactly the flags
// the deploy tooling uses: `--version` (build smoke) and `--port N`
// (ecosystem.config.js slots and the deploy canary), with API_PORT as the
// fallback default when --port is absent.
type cliOptions struct {
	version bool
	port    int // 0 means "not set": fall back to API_PORT / default
}

func parseArgs(args []string, stderr *os.File) (cliOptions, error) {
	var opts cliOptions
	fs := flag.NewFlagSet("model-relay", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.BoolVar(&opts.version, "version", false, "print the build commit and exit")
	fs.IntVar(&opts.port, "port", 0, "listen port (overrides API_PORT)")
	if err := fs.Parse(args); err != nil {
		return cliOptions{}, err
	}
	if extra := fs.Args(); len(extra) > 0 {
		return cliOptions{}, fmt.Errorf("unexpected arguments: %v", extra)
	}
	if opts.port != 0 && (opts.port < 1 || opts.port > 65535) {
		return cliOptions{}, fmt.Errorf("--port out of range: %d", opts.port)
	}
	return opts, nil
}

// versionLine is what `model-relay --version` prints: the ldflags-stamped
// commit, or "unknown" when the binary was built without stamping (the
// release build's smoke check fails on "unknown" by design).
func versionLine() string {
	commit := buildCommit
	if commit == "" {
		commit = "unknown"
	}
	return "model-relay " + commit
}

func newLegacyUploadProxy(target *url.URL) *httputil.ReverseProxy {
	proxy := httputil.NewSingleHostReverseProxy(target)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = legacyRecoveryResponseHeaderTimeout
	proxy.Transport = transport
	return proxy
}

func main() {
	// Subcommands share the binary (the release ships exactly one) but never
	// the server's flag set: `model-relay trace-backfill ...` is an operator
	// one-shot, everything else is the relay.
	if len(os.Args) > 1 && os.Args[1] == "trace-backfill" {
		if err := runTraceBackfill(os.Args[2:]); err != nil {
			fmt.Fprintf(os.Stderr, "model-relay trace-backfill: %v\n", err)
			os.Exit(1)
		}
		return
	}
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "model-relay: %v\n", err)
		os.Exit(1)
	}
}

// runTraceBackfill exports the historical inference ledgers to the trace
// sinks (and optionally deletes the exported rows). It reads the same
// environment as the relay for Postgres and the sinks, so on the host it is
// `set -a; . .env; set +a; model-relay trace-backfill --spool-dir ...`.
func runTraceBackfill(args []string) error {
	fs := flag.NewFlagSet("model-relay trace-backfill", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	spoolDir := fs.String("spool-dir", "", "directory for the backfill's own spool and cursor (REQUIRED; keep it off the live relay spool)")
	cursorPath := fs.String("cursor", "", "progress file (default <spool-dir>/cursor.json)")
	until := fs.String("until", "", "export rows started before this RFC3339 time (default now-1h)")
	batchRows := fs.Int("batch-rows", 5000, "rows per SELECT")
	lanes := fs.String("lanes", "inference,confirmation", "comma-separated lanes to export")
	del := fs.Bool("delete", false, "delete exported rows once every required sink holds them (rows younger than --retain-hours, still started, or under an unexpired grant are never deleted)")
	retainHours := fs.Int("retain-hours", 168, "never delete rows younger than this (floor 24)")
	drainWait := fs.Duration("drain-wait", 30*time.Minute, "how long to wait for uploads before deleting")
	rotateBytes := fs.Int64("rotate-bytes", 64<<20, "uncompressed bytes per object")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *spoolDir == "" {
		return errors.New("--spool-dir is required")
	}
	cfg, err := config.LoadFromEnv()
	if err != nil {
		return err
	}
	if !cfg.Traces.Enabled {
		return errors.New("INFERENCE_TRACE_ENABLED and at least one INFERENCE_TRACE_SINK are required")
	}
	var untilAt time.Time
	if *until != "" {
		untilAt, err = time.Parse(time.RFC3339, *until)
		if err != nil {
			return fmt.Errorf("--until: %w", err)
		}
	}
	logger := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slogLevel(cfg.LogLevel)}))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	bootCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	_, pool, err := postgres.NewClientWithPool(bootCtx, cfg.Postgres)
	if err != nil {
		return err
	}
	defer pool.Close()
	sinks, err := buildTraceSinks(cfg.Traces)
	if err != nil {
		return err
	}
	var laneList []string
	for _, l := range strings.Split(*lanes, ",") {
		if l = strings.TrimSpace(l); l != "" {
			laneList = append(laneList, l)
		}
	}
	summary, err := tracebackfill.Run(ctx, tracebackfill.Options{
		Pool: pool, Sinks: sinks, SpoolDir: *spoolDir, CursorPath: *cursorPath, Until: untilAt,
		BatchRows: int32(*batchRows), Lanes: laneList, Delete: *del, Retain: time.Duration(*retainHours) * time.Hour,
		DrainWait: *drainWait, RotateBytes: *rotateBytes, Logger: logger, Metrics: metrics.TraceMetrics(),
	})
	if summary != nil {
		logger.Info("trace backfill finished",
			slog.Any("rows_exported", summary.RowsExported),
			slog.Any("rows_deleted", summary.RowsDeleted),
			slog.Int("batches", summary.Batches),
			slog.Int64("dropped", summary.Dropped))
	}
	return err
}

func slogLevel(name string) slog.Level {
	switch name {
	case "DEBUG", "NOTSET":
		return slog.LevelDebug
	case "WARN", "WARNING":
		return slog.LevelWarn
	case "ERROR", "CRITICAL", "FATAL":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

func run() error {
	opts, err := parseArgs(os.Args[1:], os.Stderr)
	if err != nil {
		return err
	}
	if opts.version {
		// Must work with no environment at all: the build script's smoke
		// check runs the bare binary in an empty staging dir.
		fmt.Println(versionLine())
		return nil
	}

	cfg, err := config.LoadFromEnv()
	if err != nil {
		// Fail boot loudly: a relay with a half-parsed environment must
		// never come up looking healthy.
		return err
	}
	if opts.port != 0 {
		// --port beats API_PORT: pm2 slot args and the deploy canary rely
		// on this to pin each process to its own port.
		cfg.Port = opts.port
	}

	logger := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{
		Level: slogLevel(cfg.LogLevel),
	}))
	slog.SetDefault(logger)

	// Resolved once at process start, like the Python __main__.
	commit := server.ResolveCommitHash()
	logger.Info("model-relay starting",
		slog.String("commit", commit),
		slog.String("role", cfg.Role),
		slog.Bool("inference_proxy_enabled", cfg.Inference.Enabled),
	)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	bootCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	_, pool, err := postgres.NewClientWithPool(bootCtx, cfg.Postgres)
	if err != nil {
		return err
	}
	defer pool.Close()
	pprofserver.Start(ctx, logger, "model-relay", cfg.Port)

	prober := chain.NewPylonClient(cfg.Chain)

	queries := postgres.New(pool)
	settings := inference.NewSettingsResolver(queries, logger)
	if err := settings.Refresh(bootCtx); err != nil {
		logger.Warn("initial inference policy refresh failed; using shipped defaults",
			slog.String("error", err.Error()))
	}
	settingsErrors, err := settings.StartRefresh(ctx)
	if err != nil {
		return fmt.Errorf("start inference policy refresh: %w", err)
	}
	go func() {
		for refreshErr := range settingsErrors {
			logger.Error("inference policy refresh stopped", slog.String("error", refreshErr.Error()))
		}
	}()
	// Inference trace capture: spool on local disk, ship to every sink.
	// Boot fails loudly if a configured sink cannot be reached -- a relay that
	// silently captures nothing is worse than one that refuses to start --
	// and the spooler/uploader are drained AFTER the HTTP server so the last
	// in-flight settlements are recorded before the process exits.
	var recorder traces.Recorder
	var spooler *traces.Spooler
	var uploader *traces.Uploader
	if cfg.Traces.Enabled {
		var err error
		spooler, uploader, err = startTraceCapture(ctx, bootCtx, cfg, logger, commit)
		if err != nil {
			return err
		}
		recorder = spooler
	}
	handlers := inference.NewHandlers(&inference.Deps{
		Cfg:      cfg,
		Logger:   logger,
		Pool:     pool,
		Queries:  queries,
		Permits:  prober,
		Upstream: inference.NewUpstreamClient(cfg.Inference),
		Settings: settings,
		Traces:   recorder,
	})
	legacyURL, err := url.Parse(cfg.Upload.LegacyBaseURL)
	if err != nil {
		return fmt.Errorf("parse upload legacy base URL: %w", err)
	}
	legacy := newLegacyUploadProxy(legacyURL)
	legacy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, proxyErr error) {
		logger.Warn("upload recovery proxy failed", slog.String("error", proxyErr.Error()))
		relayhttp.WriteHTTPError(w, r, http.StatusServiceUnavailable, "upload payment recovery unavailable; retry shortly", nil)
	}
	uploadHandlers := upload.NewHandlers(&upload.Deps{
		Cfg: cfg, Logger: logger, Pool: pool, Queries: queries,
		Registration: prober, Legacy: legacy,
	})

	srv := server.New(cfg, logger, pool, prober, commit,
		server.WithInferenceHandlers(handlers), server.WithUploadHandlers(uploadHandlers))
	runErr := srv.Run(ctx)
	if spooler != nil {
		// The server has drained: every settle has run. Flush, rotate and make
		// one last shipping pass inside pm2's kill_timeout budget; whatever
		// does not make it stays on disk for the next process.
		drainCtx, cancelDrain := context.WithTimeout(context.Background(), traceDrainTimeout)
		defer cancelDrain()
		if err := spooler.Close(drainCtx); err != nil {
			logger.Warn("trace spool close", slog.String("error", err.Error()))
		}
		uploader.Drain(drainCtx)
	}
	return runErr
}

// traceDrainTimeout bounds the post-server flush + final upload pass. The
// server drain itself can take TimeoutSeconds+5 (≤125s); pm2 kills at 135s,
// so this must stay small.
const traceDrainTimeout = 8 * time.Second

// startTraceCapture builds the sinks from config, verifies each bucket,
// recovers any spool left by the previous process and starts shipping.
func startTraceCapture(ctx, bootCtx context.Context, cfg *config.Config, logger *slog.Logger, commit string) (*traces.Spooler, *traces.Uploader, error) {
	sinks, err := buildTraceSinks(cfg.Traces)
	if err != nil {
		return nil, nil, err
	}
	for _, sink := range sinks {
		if err := sink.Ensure(bootCtx); err != nil {
			return nil, nil, fmt.Errorf("trace sink %s: %w", sink.Name(), err)
		}
	}
	spooler, err := traces.NewSpooler(traces.SpoolOptions{
		Dir:            cfg.Traces.SpoolDir,
		RotateBytes:    cfg.Traces.RotateBytes,
		RotateInterval: cfg.Traces.RotateInterval,
		MaxSpoolBytes:  cfg.Traces.MaxSpoolBytes,
		QueueSize:      cfg.Traces.QueueSize,
		Instance:       fmt.Sprintf("%s:%d", hostnameOr("relay"), cfg.Port),
		Commit:         commit,
		Logger:         logger,
		Metrics:        metrics.TraceMetrics(),
	})
	if err != nil {
		return nil, nil, err
	}
	uploader, err := traces.NewUploader(spooler, traces.UploaderOptions{
		Sinks:   sinks,
		Logger:  logger,
		Metrics: metrics.TraceMetrics(),
	})
	if err != nil {
		return nil, nil, err
	}
	uploader.Start(ctx)
	names := make([]string, 0, len(sinks))
	for _, s := range sinks {
		names = append(names, s.Name())
	}
	logger.Info("inference trace capture enabled",
		slog.String("spool_dir", cfg.Traces.SpoolDir),
		slog.Any("sinks", names),
		slog.Bool("embedding_vectors", cfg.Traces.EmbeddingVectors))
	return spooler, uploader, nil
}

func buildTraceSinks(tc config.TraceConfig) ([]traces.Sink, error) {
	client := &http.Client{Timeout: 10 * time.Minute}
	sinks := make([]traces.Sink, 0, len(tc.Sinks))
	for _, sc := range tc.Sinks {
		sink, err := traces.NewS3Sink(traces.S3Config{
			Name: sc.Name, Endpoint: sc.Endpoint, Region: sc.Region, Bucket: sc.Bucket,
			AccessKeyID: sc.AccessKeyID, SecretAccessKey: sc.SecretAccessKey,
			Required: sc.Required, PathStyle: sc.PathStyle, Prefix: sc.Prefix,
		}, client)
		if err != nil {
			return nil, err
		}
		sinks = append(sinks, sink)
	}
	return sinks, nil
}

func hostnameOr(def string) string {
	host, err := os.Hostname()
	if err != nil || host == "" {
		return def
	}
	return host
}
