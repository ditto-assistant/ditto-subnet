package longmemeval

import (
	"errors"
	"sort"
)

// ExecutionFailure names where inside Execute a confirmation run broke.
//
// The confirmation handler forwards a bounded class to the validator so a
// failure is readable off-host. Above this layer the most it could say was
// "dimension_execution": correct, and true of every failure from dataset load
// through final evidence. Measured 2026-10-28 on bundle 2ede5da4 -- the stage
// arrived, and it named one call with roughly sixty failure points beneath it,
// none of which logged its cause. This type carries the point itself.
//
// Phase and Kind come only from the literals in this file, never from a wrapped
// error's text: the wrapped errors can carry submitted harness content, and the
// class crosses a boundary into a signed, operator-visible ledger.
type ExecutionFailure struct {
	Phase string
	Kind  string
	err   error
}

func (failure *ExecutionFailure) Error() string {
	if failure.err == nil {
		return "LongMemEval execution failed"
	}
	return failure.err.Error()
}

func (failure *ExecutionFailure) Unwrap() error { return failure.err }

func executionFailure(phase, kind string, err error) error {
	if err == nil {
		return nil
	}
	return &ExecutionFailure{Phase: phase, Kind: kind, err: err}
}

// accountingFailure wraps a provider-accounting check failure with its kind,
// keeping the exact message the caller formatted.
func accountingFailure(phase string, accountingErr error, err error) error {
	return executionFailure(phase, accountingKind(accountingErr), err)
}

// accountingError is a provider-meter check that failed. Its message is
// unchanged from the untyped error it replaces; only the kind is new.
type accountingError struct {
	kind    string
	message string
	cause   error
}

func (failure *accountingError) Error() string { return failure.message }

func (failure *accountingError) Unwrap() error { return failure.cause }

func newAccountingError(kind, message string) error {
	return &accountingError{kind: kind, message: message}
}

func wrapAccountingError(kind string, cause error) error {
	if cause == nil {
		return nil
	}
	return &accountingError{kind: kind, message: cause.Error(), cause: cause}
}

func accountingKind(err error) string {
	var typed *accountingError
	if errors.As(err, &typed) {
		return typed.kind
	}
	return ""
}

// Phases and the accounting kinds each can produce. Only reachable pairs are
// listed: a class that no code path can emit is vocabulary a reviewer has to
// reason about for nothing.
var (
	executionPhasesWithoutKind = []string{
		"aggregate",
		"dataset",
		"evidence",
		"judge_budget",
		"judge_opaque",
		"projection",
		"reader_budget",
		"run_opaque",
		"run_unreceipted",
		"seed_incomplete",
		"seed_opaque",
		"setup",
		"time_budget",
	}
	snapshotKinds = []string{
		"cost_source",
		"fallback_used",
		"identity_drift",
		"lane_coverage",
		"meter_unavailable",
		"provider_evidence",
		"request_free_accounting",
	}
	monotonicKinds  = []string{"receipt_changed", "receipt_uncommitted", "regressed"}
	accountingKinds = map[string][]string{
		"accounting_initial": append(append([]string(nil), snapshotKinds...), "nonzero_start"),
		"accounting_seed":    append(append([]string(nil), snapshotKinds...), monotonicKinds...),
		"accounting_run":     append(append([]string(nil), snapshotKinds...), monotonicKinds...),
		"accounting_judge":   append(append([]string(nil), snapshotKinds...), monotonicKinds...),
		"accounting_final": {
			"cost_source", "fallback_used", "identity_drift", "mixed_zero_lanes",
			"provider_evidence", "request_free_accounting",
		},
	}
)

const executionFailureClassPrefix = "longmem_eval_"

// ExecutionFailureClass returns the bounded wire class for an Execute failure,
// or false when err carries none. A phase with an unrecognized kind degrades to
// the phase alone rather than inventing a slug.
func ExecutionFailureClass(err error) (string, bool) {
	var failure *ExecutionFailure
	if !errors.As(err, &failure) {
		return "", false
	}
	allowed := executionFailureClassSet()
	if failure.Kind != "" {
		if class := executionFailureClassPrefix + failure.Phase + "_" + failure.Kind; allowed[class] {
			return class, true
		}
	}
	if class := executionFailureClassPrefix + failure.Phase; allowed[class] {
		return class, true
	}
	return "", false
}

// ExecutionFailureClasses is the complete vocabulary ExecutionFailureClass can
// return, sorted. Platform and the validator must accept exactly this set.
func ExecutionFailureClasses() []string {
	set := executionFailureClassSet()
	classes := make([]string, 0, len(set))
	for class := range set {
		classes = append(classes, class)
	}
	sort.Strings(classes)
	return classes
}

func executionFailureClassSet() map[string]bool {
	set := make(map[string]bool)
	for _, phase := range executionPhasesWithoutKind {
		set[executionFailureClassPrefix+phase] = true
	}
	for phase, kinds := range accountingKinds {
		// The phase alone is the fallback for an untyped accounting error.
		set[executionFailureClassPrefix+phase] = true
		for _, kind := range kinds {
			set[executionFailureClassPrefix+phase+"_"+kind] = true
		}
	}
	return set
}
