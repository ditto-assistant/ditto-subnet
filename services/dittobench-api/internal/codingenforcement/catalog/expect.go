package catalog

import (
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

// Expectation types. Each has a closed key set in the catalog and a closed
// observed shape in a record; matched is always recomputed, never trusted.
const (
	ExpectOutcomeIn         = "outcome_in"
	ExpectExact             = "exact"
	ExpectProfileEqual      = "profile_equal"
	ExpectBounded           = "bounded"
	ExpectSupervisorTimeout = "supervisor_timeout"
	ExpectControl           = "control"
	ExpectSubordinateIDs    = "subordinate_ids"
	// ExpectZeroRetained is an exact assertion that a container policy keeps
	// zero bytes of candidate output (Peyton, 2026-09-15: hosted grading). The
	// candidate must have emitted at least the bound limit, so an idle or
	// crashed writer, which also retains nothing, never counts.
	ExpectZeroRetained = "zero_retained"
)

var expectKeys = map[string][]string{
	ExpectOutcomeIn:         {"accept", "type"},
	ExpectExact:             {"type", "value"},
	ExpectProfileEqual:      {"type"},
	ExpectBounded:           {"floor", "tolerance", "type"},
	ExpectSupervisorTimeout: {"tolerance", "type"},
	ExpectControl:           {"result", "type"},
	ExpectSubordinateIDs:    {"gid", "type", "uid"},
	ExpectZeroRetained:      {"type"},
}

// Observed strings are short lowercase names; no address can be recorded.
var observedString = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,31}$`)

var zeroSHA256 = strings.Repeat("0", 64)

// ErrObservedShape marks an observation whose shape the expectation refuses.
var ErrObservedShape = errors.New("observed value is malformed")

// Expectation is one probe's catalog expectation.
type Expectation struct {
	Type      string
	Accept    []string
	Value     map[string]any
	Tolerance string
	Floor     string
	Result    string
	UID       int64
	GID       int64
}

// SubordinateIDs is the host's subordinate uid/gid range from the record.
type SubordinateIDs struct {
	UIDStart int64
	UIDCount int64
	GIDStart int64
	GIDCount int64
}

// hostedTestGroups is the hosted grading profile's fixed test group order.
var hostedTestGroups = []string{"hidden", "visible"}

// UnmarshalJSON enforces the closed key set for the expectation type.
func (e *Expectation) UnmarshalJSON(raw []byte) error {
	decoded, err := Decode(raw)
	if err != nil {
		return err
	}
	object, ok := decoded.(map[string]any)
	if !ok {
		return errors.New("expect is not an object")
	}
	kind, ok := object["type"].(string)
	if !ok {
		return errors.New("expect type is unknown")
	}
	keys, ok := expectKeys[kind]
	if !ok {
		return errors.New("expect type is unknown")
	}
	present := make([]string, 0, len(object))
	for key := range object {
		present = append(present, key)
	}
	slices.Sort(present)
	if !slices.Equal(present, keys) {
		return errors.New("expect keys are not the closed set")
	}
	*e = Expectation{Type: kind}
	switch kind {
	case ExpectOutcomeIn:
		list, ok := object["accept"].([]any)
		if !ok {
			return errors.New("accepted outcomes are malformed")
		}
		for _, item := range list {
			text, ok := item.(string)
			if !ok {
				return errors.New("accepted outcomes are malformed")
			}
			e.Accept = append(e.Accept, text)
		}
	case ExpectExact:
		value, ok := object["value"].(map[string]any)
		if !ok {
			return errors.New("exact value is malformed")
		}
		e.Value = value
	case ExpectBounded, ExpectSupervisorTimeout:
		if e.Tolerance, ok = object["tolerance"].(string); !ok {
			return errors.New("tolerance is malformed")
		}
		if kind == ExpectBounded {
			if e.Floor, ok = object["floor"].(string); !ok {
				return errors.New("floor is malformed")
			}
		}
	case ExpectControl:
		if e.Result, ok = object["result"].(string); !ok {
			return errors.New("result is malformed")
		}
	case ExpectSubordinateIDs:
		uid, uidOK := nonNegative(object["uid"])
		gid, gidOK := nonNegative(object["gid"])
		if !uidOK || !gidOK {
			return errors.New("candidate ids are malformed")
		}
		e.UID, e.GID = uid, gid
	}
	return nil
}

// MarshalJSON writes the closed catalog form.
func (e Expectation) MarshalJSON() ([]byte, error) {
	object := map[string]any{"type": e.Type}
	switch e.Type {
	case ExpectOutcomeIn:
		object["accept"] = e.Accept
	case ExpectExact:
		object["value"] = e.Value
	case ExpectBounded:
		object["tolerance"], object["floor"] = e.Tolerance, e.Floor
	case ExpectSupervisorTimeout:
		object["tolerance"] = e.Tolerance
	case ExpectControl:
		object["result"] = e.Result
	case ExpectSubordinateIDs:
		object["uid"], object["gid"] = e.UID, e.GID
	}
	return Canonical(object)
}

func (e Expectation) validate(outcomes map[string]bool) error {
	switch e.Type {
	case ExpectOutcomeIn:
		if len(e.Accept) == 0 || !slices.IsSorted(e.Accept) {
			return errors.New("accepted outcomes are malformed")
		}
		for index, outcome := range e.Accept {
			if !outcomes[outcome] || (index > 0 && e.Accept[index-1] == outcome) {
				return errors.New("accepted outcomes are malformed")
			}
		}
	case ExpectExact:
		if len(e.Value) == 0 {
			return errors.New("exact value is malformed")
		}
		for _, item := range e.Value {
			if !exactValue(item) {
				return errors.New("exact value is malformed")
			}
		}
	case ExpectBounded:
		ceiling, ceilingOK := VersionedTolerances.Permille(e.Tolerance)
		floor, floorOK := VersionedTolerances.Permille(e.Floor)
		if !ceilingOK || !floorOK || !strings.Contains(e.Tolerance, "_max_permille_of_") ||
			e.Floor != strings.Replace(e.Tolerance, "_max_", "_min_", 1) {
			return errors.New("tolerance is unknown")
		}
		if floor < 1 || floor > ceiling {
			return errors.New("tolerance floor exceeds its ceiling")
		}
	case ExpectSupervisorTimeout:
		if e.Tolerance != "timeout_elapsed_max_permille_of_deadline" {
			return errors.New("tolerance is unknown")
		}
	case ExpectControl:
		if e.Result != "all_pass" && e.Result != "some_fail" && e.Result != "timeout" {
			return errors.New("result is unknown")
		}
	case ExpectSubordinateIDs:
		if e.UID < 1 || e.UID >= SubordinateMinCount || e.GID < 1 || e.GID >= SubordinateMinCount {
			return errors.New("candidate ids are malformed")
		}
	case ExpectProfileEqual, ExpectZeroRetained:
	default:
		return errors.New("expect type is unknown")
	}
	return nil
}

func exactValue(item any) bool {
	switch typed := item.(type) {
	case bool:
		return true
	case json.Number:
		_, ok := nonNegative(typed)
		return ok
	case []any:
		return stringList(typed)
	}
	return false
}

func stringList(list []any) bool {
	for _, entry := range list {
		text, ok := entry.(string)
		if !ok || !observedString.MatchString(text) {
			return false
		}
	}
	return true
}

func nonNegative(value any) (int64, bool) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, false
	}
	parsed, err := strconv.ParseInt(string(number), 10, 64)
	if err != nil || parsed < 0 {
		return 0, false
	}
	return parsed, true
}

func observedObject(observed any, keys ...string) (map[string]any, error) {
	object, ok := observed.(map[string]any)
	if !ok || len(object) != len(keys) {
		return nil, ErrObservedShape
	}
	for _, key := range keys {
		if _, ok := object[key]; !ok {
			return nil, ErrObservedShape
		}
	}
	return object, nil
}

func integers(object map[string]any, keys ...string) ([]int64, error) {
	values := make([]int64, len(keys))
	for index, key := range keys {
		value, ok := nonNegative(object[key])
		if !ok {
			return nil, ErrObservedShape
		}
		values[index] = value
	}
	return values, nil
}

// atLeastPermille reports measured*1000 >= limit*permille without overflow.
func atLeastPermille(measured, limit, permille int64) bool {
	left := new(big.Int).Mul(big.NewInt(measured), big.NewInt(1000))
	right := new(big.Int).Mul(big.NewInt(limit), big.NewInt(permille))
	return left.Cmp(right) >= 0
}

// withinPermille reports measured*1000 <= limit*permille without overflow.
func withinPermille(measured, limit, permille int64) bool {
	left := new(big.Int).Mul(big.NewInt(measured), big.NewInt(1000))
	right := new(big.Int).Mul(big.NewInt(limit), big.NewInt(permille))
	return left.Cmp(right) <= 0
}

// Evaluate recomputes matched for one observation decoded by Decode. A shape
// the expectation refuses returns an error wrapping ErrObservedShape.
func Evaluate(expect Expectation, observed any, subordinate SubordinateIDs, outcomes map[string]bool, tolerances Tolerances) (bool, error) {
	switch expect.Type {
	case ExpectOutcomeIn:
		object, err := observedObject(observed, "outcome")
		if err != nil {
			return false, err
		}
		outcome, ok := object["outcome"].(string)
		if !ok || !outcomes[outcome] {
			return false, fmt.Errorf("%w: outcome is unknown", ErrObservedShape)
		}
		return slices.Contains(expect.Accept, outcome), nil
	case ExpectExact:
		keys := make([]string, 0, len(expect.Value))
		for key := range expect.Value {
			keys = append(keys, key)
		}
		object, err := observedObject(observed, keys...)
		if err != nil {
			return false, err
		}
		matched := true
		for key, want := range expect.Value {
			got := object[key]
			switch typed := want.(type) {
			case []any:
				list, ok := got.([]any)
				if !ok || !stringList(list) {
					return false, ErrObservedShape
				}
				if !slices.Equal(list, typed) {
					matched = false
				}
			case bool:
				value, ok := got.(bool)
				if !ok {
					return false, ErrObservedShape
				}
				matched = matched && value == typed
			default:
				wantNumber, _ := nonNegative(want)
				value, ok := nonNegative(got)
				if !ok {
					return false, ErrObservedShape
				}
				matched = matched && value == wantNumber
			}
		}
		return matched, nil
	case ExpectProfileEqual:
		object, err := observedObject(observed, "cgroup", "profile")
		if err != nil {
			return false, err
		}
		values, err := integers(object, "cgroup", "profile")
		if err != nil {
			return false, err
		}
		return values[1] >= 1 && values[0] == values[1], nil
	case ExpectZeroRetained:
		object, err := observedObject(observed, "emitted_bytes", "limit", "retained_bytes")
		if err != nil {
			return false, err
		}
		values, err := integers(object, "emitted_bytes", "limit", "retained_bytes")
		if err != nil {
			return false, err
		}
		emitted, limit, retained := values[0], values[1], values[2]
		return limit >= 1 && emitted >= limit && retained == 0, nil
	case ExpectBounded:
		object, err := observedObject(observed, "enforced", "limit", "measured")
		if err != nil {
			return false, err
		}
		enforced, ok := object["enforced"].(bool)
		if !ok {
			return false, ErrObservedShape
		}
		values, err := integers(object, "limit", "measured")
		if err != nil {
			return false, err
		}
		permille, ok := tolerances.Permille(expect.Tolerance)
		floor, floorOK := tolerances.Permille(expect.Floor)
		if !ok || !floorOK {
			return false, errors.New("tolerance is unknown")
		}
		limit, measured := values[0], values[1]
		return enforced && limit >= 1 && measured >= 1 && withinPermille(measured, limit, permille) &&
			atLeastPermille(measured, limit, floor), nil
	case ExpectSupervisorTimeout:
		object, err := observedObject(observed, "deadline_ms", "elapsed_ms", "exit_code", "live_processes", "test_group")
		if err != nil {
			return false, err
		}
		values, err := integers(object, "deadline_ms", "elapsed_ms", "exit_code", "live_processes")
		if err != nil {
			return false, err
		}
		group, ok := object["test_group"].(string)
		deadline, elapsed, exitCode, live := values[0], values[1], values[2], values[3]
		if exitCode > 255 || !ok || !slices.Contains(hostedTestGroups, group) {
			return false, ErrObservedShape
		}
		return exitCode == 124 && live == 0 && deadline >= 1 && elapsed >= deadline &&
			withinPermille(elapsed, deadline, tolerances.TimeoutElapsedMaxPermilleOfDeadline), nil
	case ExpectControl:
		object, err := observedObject(observed, "passed", "suite_sha256", "timed_out", "total")
		if err != nil {
			return false, err
		}
		values, err := integers(object, "passed", "total")
		if err != nil {
			return false, err
		}
		suite, suiteOK := object["suite_sha256"].(string)
		timedOut, timedOK := object["timed_out"].(bool)
		passed, total := values[0], values[1]
		if passed > total || !suiteOK || !sha256Hex.MatchString(suite) || suite == zeroSHA256 || !timedOK {
			return false, ErrObservedShape
		}
		if expect.Result == "timeout" {
			return timedOut, nil
		}
		// Fewer than two tests, or a wrong control passing none, is also what a
		// crashed grader reports; neither proves the suite ran.
		if timedOut || total < 2 {
			return false, nil
		}
		if expect.Result == "all_pass" {
			return passed == total, nil
		}
		return passed >= 1 && passed < total, nil
	case ExpectSubordinateIDs:
		object, err := observedObject(observed, "host_gid", "host_uid")
		if err != nil {
			return false, err
		}
		values, err := integers(object, "host_gid", "host_uid")
		if err != nil {
			return false, err
		}
		hostGID, hostUID := values[0], values[1]
		return expect.UID < subordinate.UIDCount && expect.GID < subordinate.GIDCount &&
			hostUID == subordinate.UIDStart+expect.UID-1 && hostGID == subordinate.GIDStart+expect.GID-1, nil
	}
	return false, errors.New("expect type is unknown")
}
