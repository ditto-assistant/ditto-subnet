# Pinned model-relay compatibility source

This directory preserves the final reviewed `cmd/model-relay` implementation
required by compatibility-epoch 2 validator updaters. `UPSTREAM_REVISION`
records the former `ditto-assistant/dittobench-api` revision from which the
compatibility source was copied.

The compatibility-epoch 2 source intentionally carries one monorepo-owned
operational overlay: it builds with the pinned Go 1.26.6 toolchain and starts a
non-configurable, loopback-only pprof listener. The provider, HTTP, and failure
contracts remain unchanged, and the profiler never enters the request plane.
This is rollback compatibility code, not the active Platform inference path.

`SHA256SUMS` pins every module, Go source, and test file used by the compatibility
binary. Update it only with an explicit compatibility decision and validation;
the release fails closed when a pinned file changes. A broader replacement
still requires a new audited source revision and updated provenance.
