// Package codinggateway composes one shadow-only, ticket-bound coding
// inference capability from the reviewed Platform client, durable journal,
// and Luna relay.
//
// The package owns no listener, grant exchange transport, harness lifecycle,
// scheduler, score, deployment flag, or provider credential. Callers must
// publish the returned relay through a source-bound capability publisher and
// must durably activate authoring before calling Activate.
package codinggateway
