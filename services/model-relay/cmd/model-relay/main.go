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
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/ditto-assistant/model-relay/internal/chain"
	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/inference"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/server"
	"github.com/ditto-assistant/model-relay/internal/upload"
)

// buildCommit is stamped by the release build via
// -ldflags "-X main.buildCommit=<40-char sha>". It backs `--version` only;
// the /health commit field is sourced from DITTO_BUILD_COMMIT at runtime
// (the deploy scripts export both from the same source-commit marker).
var buildCommit string

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

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "model-relay: %v\n", err)
		os.Exit(1)
	}
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

	prober := chain.NewPylonClient(cfg.Chain)

	queries := postgres.New(pool)
	handlers := inference.NewHandlers(&inference.Deps{
		Cfg:      cfg,
		Logger:   logger,
		Pool:     pool,
		Queries:  queries,
		Permits:  prober,
		Upstream: inference.NewUpstreamClient(cfg.Inference),
		Settings: inference.NewSettingsResolver(queries, logger),
	})
	legacyURL, err := url.Parse(cfg.Upload.LegacyBaseURL)
	if err != nil {
		return fmt.Errorf("parse upload legacy base URL: %w", err)
	}
	legacy := httputil.NewSingleHostReverseProxy(legacyURL)
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
	return srv.Run(ctx)
}
