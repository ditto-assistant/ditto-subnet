package codinggooracle

import (
	"context"
	"errors"
	"go/token"
	"go/types"
	"math"
	"reflect"
	"testing"
	"time"
)

type fakeCandidate struct {
	call   func(string, []Value) ([]Value, error)
	method func(Value, string, []Value) ([]Value, error)
	close  func() error
}

func (f *fakeCandidate) Call(_ context.Context, name string, args []Value) ([]Value, error) {
	return f.call(name, args)
}
func (f *fakeCandidate) Method(_ context.Context, receiver Value, name string, args []Value) ([]Value, error) {
	if f.method == nil {
		return nil, ErrCandidate
	}
	return f.method(receiver, name, args)
}
func (f *fakeCandidate) Close() error {
	if f.close != nil {
		return f.close()
	}
	return nil
}
func mustValue(t *testing.T, typ types.Type, data any) Value {
	t.Helper()
	value, err := NewValue(typ, data)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func TestEvaluatorOwnsAssertionsAndReapsBeforeCompletion(t *testing.T) {
	source := `package subject;import "testing";func TestAdd(t *testing.T){got:=Add(2,3);if got!=5{t.Fatal("private expected value")}}`
	suite, err := Compile([]byte(source), configuration())
	if err != nil {
		t.Fatal(err)
	}
	for _, answer := range []int{5, 99} {
		closed := false
		calls := 0
		result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
			return &fakeCandidate{
				call: func(name string, args []Value) ([]Value, error) {
					calls++
					if name != "Add" || len(args) != 2 || args[0].Data() != 2 || args[1].Data() != 3 {
						t.Fatal("private assertion entered API channel")
					}
					return []Value{mustValue(t, types.Typ[types.Int], answer)}, nil
				},
				close: func() error { closed = true; return nil },
			}, nil
		})
		if err != nil || !result.Completed || !closed || calls != 1 || result.Total != 1 {
			t.Fatal("incomplete trusted evaluation", err, result)
		}
		want := 0
		if answer == 5 {
			want = 1
		}
		if result.Passed != want {
			t.Fatal("candidate output became test count")
		}
	}
}

func TestHelpersAndTimeOperationsStayInTrustedParent(t *testing.T) {
	config := configuration()
	config.Functions = append(config.Functions, "Window")
	config.CandidateSources = append(config.CandidateSources, Source{Name: "window.go", Body: []byte(`package subject;import "time";func Window(a,b time.Time)bool{return a.Before(b)}`)})
	source := `package subject;import("testing";"time")
func has(values []int,want int)bool{for _,v:=range values{if v==want{return true}};return false}
func TestThings(t *testing.T){
 if !has([]int{Add(2,3)},5){t.Fatal("private helper")}
 a:=time.Date(2001,time.January,1,0,0,0,0,time.UTC)
 b:=a.Add(time.Second).In(time.FixedZone("synthetic",3600))
 if !Window(a,b){t.Fatal("private time expectation")}
}`
	suite, err := Compile([]byte(source), config)
	if err != nil {
		t.Fatal(err)
	}
	calls := []string{}
	result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
		return &fakeCandidate{call: func(name string, args []Value) ([]Value, error) {
			calls = append(calls, name)
			if name == "Add" {
				return []Value{mustValue(t, types.Typ[types.Int], 5)}, nil
			}
			if name != "Window" {
				t.Fatal("private helper delegated")
			}
			a, b := args[0].Data().(time.Time), args[1].Data().(time.Time)
			if b.Sub(a) != time.Second || b.Location().String() != "synthetic" {
				t.Fatal("time semantics changed")
			}
			return []Value{mustValue(t, types.Typ[types.Bool], true)}, nil
		}}, nil
	})
	if err != nil || result.Passed != 1 || !reflect.DeepEqual(calls, []string{"Add", "Window"}) {
		t.Fatal("trusted helper evaluation", err, result)
	}
}

func TestMultipleResultsNilErrorsAndShortCircuit(t *testing.T) {
	config := configuration()
	config.Functions = append(config.Functions, "Parse")
	config.CandidateSources[0].Body = append(config.CandidateSources[0].Body, []byte(`
func Parse(value string)(string,error){return value,nil}`)...)
	source := `package subject;import "testing";func TestParse(t *testing.T){got,err:=Parse("input");if err!=nil&&err.Error()!="expected"{t.Fatal("bad error")};if got!="output"{t.Fatal("bad value")}}`
	suite, err := Compile([]byte(source), config)
	if err != nil {
		t.Fatal(err)
	}
	result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
		return &fakeCandidate{
			call: func(_ string, _ []Value) ([]Value, error) {
				return []Value{mustValue(t, types.Typ[types.String], "output"), mustValue(t, types.Universe.Lookup("error").Type(), nil)}, nil
			},
			method: func(Value, string, []Value) ([]Value, error) {
				t.Fatal("nil error was evaluated despite short circuit")
				return nil, nil
			},
		}, nil
	})
	if err != nil || result.Passed != 1 {
		t.Fatal("tuple/short circuit", err, result)
	}
}

func TestFatalStopsButErrorContinuesAndEachTestGetsFreshClient(t *testing.T) {
	for _, method := range []string{"Fatal", "Error"} {
		config := configuration()
		config.ExpectedTests = 2
		source := `package subject;import "testing";func TestFirst(t *testing.T){if Add(1,1)!=2{t.` + method + `("bad")};if Add(2,2)!=4{t.Fatal("bad")}};func TestSecond(t *testing.T){if Add(3,3)!=6{t.Fatal("bad")}}`
		suite, err := Compile([]byte(source), config)
		if err != nil {
			t.Fatal(err)
		}
		events := []string{}
		calls := 0
		result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
			events = append(events, "new")
			return &fakeCandidate{
				call: func(_ string, args []Value) ([]Value, error) {
					calls++
					answer := args[0].Data().(int) + args[1].Data().(int)
					if calls == 1 {
						answer = 99
					}
					return []Value{mustValue(t, types.Typ[types.Int], answer)}, nil
				},
				close: func() error { events = append(events, "dead"); return nil },
			}, nil
		})
		want := 2
		if method == "Error" {
			want = 3
		}
		if err != nil || result.Passed != 1 || calls != want || !reflect.DeepEqual(events, []string{"new", "dead", "new", "dead"}) {
			t.Fatal("lifecycle/assertion semantics", err, result, events)
		}
	}
}

func TestCloseAndStartupFailuresCannotProduceCompletedReport(t *testing.T) {
	suite, err := Compile([]byte(`package subject;import "testing";func TestAdd(t *testing.T){if Add(1,1)!=2{t.Fatal("bad")}}`), configuration())
	if err != nil {
		t.Fatal(err)
	}
	for _, factory := range []Factory{
		func(context.Context) (Candidate, error) { return nil, errors.New("synthetic startup failure") },
		func(context.Context) (Candidate, error) {
			return &fakeCandidate{call: func(string, []Value) ([]Value, error) { return []Value{mustValue(t, types.Typ[types.Int], 2)}, nil }, close: func() error { return errors.New("synthetic cleanup failure") }}, nil
		},
	} {
		result, err := Run(context.Background(), suite, factory)
		if err != ErrRuntime || result.Completed {
			t.Fatal("infrastructure failure reported completion")
		}
	}
}

func TestArgumentSnapshotsPreserveSliceMutationWithoutReplacingHeaders(t *testing.T) {
	typ := types.NewSlice(types.Typ[types.String])
	original := mustValue(t, typ, []string{"a", "b"})
	alias := original
	snapshot := mustValue(t, typ, []string{"updated", "b"})
	if err := ApplyArgumentSnapshot(original, snapshot); err != nil || alias.Data().([]string)[0] != "updated" {
		t.Fatal("mutation not reflected through alias")
	}
	if err := ApplyArgumentSnapshot(original, mustValue(t, typ, []string{"short"})); err != ErrCandidate {
		t.Fatal("caller slice header replaced")
	}
}

func TestGoNumericAndNilComparisonEdges(t *testing.T) {
	nan := mustValue(t, types.Typ[types.Float64], math.NaN())
	one := mustValue(t, types.Typ[types.Float64], float64(1))
	for _, operator := range []token.Token{token.LSS, token.LEQ, token.GTR, token.GEQ, token.EQL} {
		value, err := binary(operator, nan, one, types.Typ[types.Bool])
		if err != nil || value.Data() != false {
			t.Fatal("NaN comparison changed", operator, err)
		}
	}
	slice := types.NewSlice(types.Typ[types.String])
	empty := mustValue(t, slice, []string{})
	nilSlice := mustValue(t, slice, nil)
	value, err := binary(token.NEQ, empty, nilSlice, types.Typ[types.Bool])
	if err != nil || value.Data() != true {
		t.Fatal("nil versus empty slice changed", err)
	}
	value, err = binary(token.SHL, mustValue(t, types.Typ[types.Int8], int8(1)), mustValue(t, types.Typ[types.Uint], uint(7)), types.Typ[types.Int8])
	if err != nil || value.Data() != int8(-128) {
		t.Fatal("typed shift changed", err)
	}
	if _, err := binary(token.SHL, mustValue(t, types.Typ[types.Int], 1), mustValue(t, types.Typ[types.Int], -1), types.Typ[types.Int]); err != ErrCandidate {
		t.Fatal("negative shift accepted")
	}
	mapType := types.NewMap(types.Typ[types.String], types.Typ[types.Int])
	same := mustValue(t, mapType, map[string]int{"x": 1})
	if err := ApplyArgumentSnapshot(same, same); err != nil || same.Data().(map[string]int)["x"] != 1 {
		t.Fatal("aliased map snapshot destroyed values")
	}
}

func TestTransportInfrastructureFailureDoesNotBecomeCandidateScore(t *testing.T) {
	suite, err := Compile([]byte(`package subject;import "testing";func TestAdd(t *testing.T){if Add(1,1)!=2{t.Fatal("bad")}}`), configuration())
	if err != nil {
		t.Fatal(err)
	}
	closed := false
	result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
		return &fakeCandidate{call: func(string, []Value) ([]Value, error) { return nil, ErrRuntime }, close: func() error { closed = true; return nil }}, nil
	})
	if err != ErrRuntime || result.Completed || !closed {
		t.Fatal("infrastructure error misclassified", err, result)
	}
}

func TestInterfacesPreserveDynamicTypesAndTypedNil(t *testing.T) {
	anyType := types.Universe.Lookup("any").Type()
	typedNil := mustValue(t, types.NewPointer(types.Typ[types.Int]), nil)
	boxed, err := assign(typedNil, anyType)
	if err != nil {
		t.Fatal(err)
	}
	nilInterface := mustValue(t, anyType, nil)
	equal, err := binary(token.EQL, boxed, nilInterface, types.Typ[types.Bool])
	if err != nil || equal.Data() != false {
		t.Fatal("typed nil became nil interface", err)
	}
	scalar := mustValue(t, types.Typ[types.Int], 5)
	boxed, err = assign(scalar, anyType)
	if err != nil {
		t.Fatal(err)
	}
	equal, err = binary(token.EQL, boxed, scalar, types.Typ[types.Bool])
	if err != nil || equal.Data() != true {
		t.Fatal("interface scalar equality changed", err)
	}
}

func TestEmptyHelperLoopsStillConsumeBudgetAndCloseCandidate(t *testing.T) {
	config := configuration()
	config.Functions = append(config.Functions, "Huge")
	config.CandidateSources[0].Body = append(config.CandidateSources[0].Body, []byte(`
func Huge()[]int{return nil}`)...)
	suite, err := Compile([]byte(`package subject;import "testing";func scan(values []int)bool{for range values{};return true};func TestBound(t *testing.T){if !scan(Huge()){t.Fatal("bad")}}`), config)
	if err != nil {
		t.Fatal(err)
	}
	closed := false
	result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
		return &fakeCandidate{
			call: func(string, []Value) ([]Value, error) {
				return []Value{mustValue(t, types.NewSlice(types.Typ[types.Int]), make([]int, 100001))}, nil
			},
			close: func() error { closed = true; return nil },
		}, nil
	})
	if err != ErrRuntime || result.Completed || !closed {
		t.Fatal("loop budget bypassed", err, result)
	}
}

func TestPrivateHelperNamedResultsAndVoidReturn(t *testing.T) {
	source := `package subject;import "testing";func noop(){};func expected()(value int64){value=5;return};func TestHelper(t *testing.T){noop();if int64(Add(2,3))!=expected(){t.Fatal("bad")}}`
	suite, err := Compile([]byte(source), configuration())
	if err != nil {
		t.Fatal(err)
	}
	result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
		return &fakeCandidate{call: func(string, []Value) ([]Value, error) { return []Value{mustValue(t, types.Typ[types.Int], 5)}, nil }}, nil
	})
	if err != nil || result.Passed != 1 {
		t.Fatal("helper return semantics", err, result)
	}
}

func TestTransportPanicsStillCloseAndNeverComplete(t *testing.T) {
	suite, err := Compile([]byte(`package subject;import "testing";func TestAdd(t *testing.T){if Add(1,1)!=2{t.Fatal("bad")}}`), configuration())
	if err != nil {
		t.Fatal(err)
	}
	closed := false
	result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
		return &fakeCandidate{call: func(string, []Value) ([]Value, error) { panic("private transport diagnostic must not escape") }, close: func() error { closed = true; return nil }}, nil
	})
	if err != ErrRuntime || result.Completed || !closed {
		t.Fatal("transport panic classified as a score", err, result)
	}
}

func TestPartiallyStartedCandidateIsClosedOnFactoryError(t *testing.T) {
	suite, err := Compile([]byte(`package subject;import "testing";func TestAdd(t *testing.T){if Add(1,1)!=2{t.Fatal("bad")}}`), configuration())
	if err != nil {
		t.Fatal(err)
	}
	closed := false
	result, err := Run(context.Background(), suite, func(context.Context) (Candidate, error) {
		return &fakeCandidate{close: func() error { closed = true; return nil }}, ErrCandidate
	})
	if err != nil || !result.Completed || result.Passed != 0 || !closed {
		t.Fatal("partial startup leaked", err, result)
	}
}
