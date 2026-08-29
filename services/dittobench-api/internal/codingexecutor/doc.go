// Package codingexecutor implements the shadow-only container boundary for
// DittoBench Coding authoring commands and pristine grading.
//
// It has no scorer route and accepts no miner-selected executable. A pinned
// trusted supervisor image runs exact manifest commands in a fresh networkless
// container, writes a bounded nonce-bound receipt through a private control
// mount, and is removed by exact random identity after every invocation.
package codingexecutor
