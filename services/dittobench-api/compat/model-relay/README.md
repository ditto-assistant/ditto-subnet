# Frozen model-relay compatibility source

This directory preserves the final reviewed `cmd/model-relay` implementation
required by compatibility-epoch 2 validator updaters. The Go sources, module
files, and tests were copied byte-for-byte from the former
`ditto-assistant/dittobench-api` revision recorded in `UPSTREAM_REVISION`.

It is rollback compatibility code, not the active inference path. Do not update
dependencies or source in place. A replacement requires a new audited upstream
revision, updated provenance, and an explicit compatibility decision.
`SHA256SUMS` makes the release fail unless every frozen source file still
matches that audited snapshot.
