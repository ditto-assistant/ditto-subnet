package postgres

import (
	"context"
	"fmt"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ditto-assistant/model-relay/internal/config"
)

// NewClientWithPool builds the pgx pool from the relay's POSTGRES_* config
// and returns both the generated Queries and the raw pool (the pool is what
// /health pings, and any future subsystem needing LISTEN/NOTIFY shares it
// without piercing the DBTX abstraction). The pool is pinged before return
// so boot fails loudly on an unreachable database.
//
// Sizing follows the Python relay's semantics: POSTGRES_POOL_MIN_SIZE is the
// resident pool (default 2), POSTGRES_POOL_MAX_SIZE the ceiling (default
// 10). MinConns stays tiny by default because many processes share one
// Postgres and idle connections are held open.
func NewClientWithPool(ctx context.Context, cfg config.PostgresConfig) (*Queries, *pgxpool.Pool, error) {
	pcfg, err := pgxpool.ParseConfig(cfg.DSN())
	if err != nil {
		return nil, nil, fmt.Errorf("error parsing postgres connection string: %w", err)
	}
	pcfg.MinConns = cfg.PoolMinSize
	pcfg.MaxConns = cfg.PoolMaxSize
	pcfg.MaxConnLifetime = 1 * time.Hour
	pcfg.MaxConnIdleTime = 10 * time.Minute
	pcfg.HealthCheckPeriod = 1 * time.Minute
	// POSTGRES_COMMAND_TIMEOUT is asyncpg's per-statement timeout in the
	// Python relay. pgx has no per-statement timeout knob; enforce it
	// server-side so runaway statements are cancelled at the same bound.
	if cfg.CommandTimeout > 0 {
		ms := int64(cfg.CommandTimeout * 1000)
		pcfg.ConnConfig.RuntimeParams["statement_timeout"] = strconv.FormatInt(ms, 10)
	}

	pool, err := pgxpool.NewWithConfig(ctx, pcfg)
	if err != nil {
		return nil, nil, fmt.Errorf("error creating postgres connection pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, nil, fmt.Errorf("error pinging postgres database: %w", err)
	}
	return New(pool), pool, nil
}

// DB exposes the underlying DBTX (pool or transaction) for callers that need
// raw access without piercing the abstraction elsewhere.
func (q *Queries) DB() DBTX { return q.db }
