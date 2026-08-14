package postgres

// Regenerate the schema mirror from the real Alembic chain, then the query
// code from the pinned sqlc (tool directive in go.mod, v1.30.0):
//
//go:generate ../../scripts/gen-schema.sh
//go:generate go tool sqlc generate -f sqlc.yaml
