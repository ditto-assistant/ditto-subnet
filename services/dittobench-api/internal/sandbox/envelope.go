package sandbox

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// runtimeEnvelopeJSON is a byte-identical copy of the published SN118 runtime
// envelope, packages/ditto-screening-protocol/ditto_screening_protocol/data/
// runtime_envelope.json. Go cannot embed a file outside its module, so the copy
// lives here and TestRuntimeEnvelopeMatchesPublishedSource fails on drift.
//
//go:embed runtime_envelope.json
var runtimeEnvelopeJSON []byte

// RuntimeEnvelope is the subset of the published envelope the scorer enforces
// or classifies against. docs/RUNTIME-ENVELOPE.md is its prose.
type RuntimeEnvelope struct {
	Schema  string `json:"schema"`
	Harness struct {
		MemoryLimitMiB int `json:"memory_limit_mib"`
		SwapMiB        int `json:"swap_mib"`
		CPUs           int `json:"cpus"`
		PidsLimit      int `json:"pids_limit"`
		TmpfsMiB       int `json:"tmpfs_mib"`
	} `json:"harness"`
	Timing struct {
		ContainerStartSeconds int `json:"container_start_seconds"`
		HealthyWithinSeconds  int `json:"healthy_within_seconds"`
		SeedCallSeconds       int `json:"seed_call_seconds"`
		CaseCallSeconds       int `json:"case_call_seconds"`
	} `json:"timing"`
	OOM struct {
		PeakToleranceMiB      int `json:"peak_tolerance_mib"`
		HostAvailableFloorMiB int `json:"host_available_floor_mib"`
	} `json:"oom"`
}

const runtimeEnvelopeSchema = "ditto.sn118.runtime_envelope.v1"

// PublishedEnvelope is decoded once at init; a malformed embedded file is a
// build defect, so it panics rather than letting a scorer run unbounded.
var PublishedEnvelope = mustDecodeEnvelope(runtimeEnvelopeJSON)

func mustDecodeEnvelope(raw []byte) RuntimeEnvelope {
	var envelope RuntimeEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		panic(fmt.Sprintf("decode runtime envelope: %v", err))
	}
	if envelope.Schema != runtimeEnvelopeSchema ||
		envelope.Harness.CPUs <= 0 || envelope.Harness.PidsLimit <= 0 ||
		envelope.Harness.TmpfsMiB <= 0 || envelope.OOM.PeakToleranceMiB <= 0 ||
		envelope.Timing.ContainerStartSeconds <= 0 || envelope.Timing.HealthyWithinSeconds <= 0 ||
		envelope.OOM.HostAvailableFloorMiB <= 0 {
		panic("runtime envelope is incomplete")
	}
	return envelope
}

const mib = uint64(1024 * 1024)

// parseDockerMemory converts docker --memory syntax ("8192m", "3g", "512k",
// "1073741824") into bytes. It returns false for anything else.
func parseDockerMemory(value string) (uint64, bool) {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		return 0, false
	}
	multiplier := uint64(1)
	switch value[len(value)-1] {
	case 'b':
		value = value[:len(value)-1]
	case 'k':
		multiplier, value = 1024, value[:len(value)-1]
	case 'm':
		multiplier, value = mib, value[:len(value)-1]
	case 'g':
		multiplier, value = 1024*mib, value[:len(value)-1]
	}
	n, err := strconv.ParseUint(value, 10, 64)
	if err != nil || n == 0 {
		return 0, false
	}
	return n * multiplier, true
}
