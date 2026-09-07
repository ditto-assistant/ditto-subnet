//go:build linux

package codinggooracle

import (
	"go/types"
	"math"
	"reflect"
	"testing"
	"time"
)

func codecClient() *processClient {
	return &processClient{references: map[uint64]types.Type{}, locations: map[*time.Location]uint64{}}
}

func TestProcessCodecPreservesScalarBitsAndSliceShape(t *testing.T) {
	client := codecClient()
	for _, item := range []struct {
		typ  types.Type
		data any
	}{
		{types.Typ[types.Int64], int64(9223372036854775807)},
		{types.Typ[types.Uint64], uint64(18446744073709551615)},
		{types.Typ[types.Float64], math.Copysign(0, -1)},
		{types.Typ[types.Float32], float32(0.1)},
		{types.NewSlice(types.Typ[types.String]), []string(nil)},
		{types.NewSlice(types.Typ[types.String]), []string{}},
		{types.NewSlice(types.Typ[types.String]), []string{"a", ""}},
	} {
		value := mustValue(t, item.typ, item.data)
		wire, err := client.encode(value, 0)
		if err != nil {
			t.Fatal(err)
		}
		decoded, err := client.decode(wire, item.typ, 0)
		if err != nil || !reflect.DeepEqual(decoded.Data(), item.data) {
			t.Fatal("wire changed value", err)
		}
		if number, ok := item.data.(float64); ok && math.Signbit(number) != math.Signbit(decoded.Data().(float64)) {
			t.Fatal("negative zero changed")
		}
	}
}

func TestProcessCodecRejectsTypeConfusionAndMalformedValues(t *testing.T) {
	client := codecClient()
	for _, wire := range []wireValue{
		{Kind: "int64", Type: "int64", Text: "9223372036854775808"},
		{Kind: "int64", Type: "int64", Text: "01"},
		{Kind: "int64", Type: "string", Text: "1"},
		{Kind: "int64", Type: "int64", Text: "1", Reference: 2},
		{Kind: "int64", Type: "int64", Text: "1", Items: []wireValue{{Kind: "nil"}}},
		{Kind: "nil", Type: "int64"},
	} {
		if _, err := client.decode(wire, types.Typ[types.Int64], 0); err == nil {
			t.Fatal("malformed integer accepted")
		}
	}
	slice := types.NewSlice(types.Typ[types.String])
	for _, wire := range []wireValue{{Kind: "slice", Type: "[]string", Length: 1, Capacity: 0}, {Kind: "slice", Type: "[]string", Capacity: 4097}, {Kind: "nil", Type: "[]string", Text: "extra"}} {
		if _, err := client.decode(wire, slice, 0); err == nil {
			t.Fatal("malformed slice accepted")
		}
	}
}

func TestProcessCodecNilIsBoundToParameterType(t *testing.T) {
	client := codecClient()
	nilLiteral := mustValue(t, types.Typ[types.UntypedNil], nil)
	typ := types.NewSlice(types.Typ[types.String])
	bound, err := assign(nilLiteral, typ)
	if err != nil {
		t.Fatal(err)
	}
	wire, err := client.encode(bound, 0)
	if err != nil || wire.Kind != "nil" || wire.Type != "[]string" {
		t.Fatal("untyped nil reached bridge", err)
	}
}

func TestReferenceReuseCannotChangeItsDeclaredType(t *testing.T) {
	client := codecClient()
	typ := types.Universe.Lookup("error").Type()
	if _, err := client.decode(wireValue{Kind: "reference", Type: "error", Reference: 1}, typ, 0); err != nil {
		t.Fatal(err)
	}
	if _, err := client.decode(wireValue{Kind: "reference", Type: "*int", Reference: 1}, types.NewPointer(types.Typ[types.Int]), 0); err == nil {
		t.Fatal("reference type changed")
	}
}
