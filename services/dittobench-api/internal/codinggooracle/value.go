package codinggooracle

import (
	"errors"
	"go/constant"
	"go/types"
	"reflect"
	"time"
)

var ErrCandidate = errors.New("candidate Go API failed")
var ErrRuntime = errors.New("trusted Go oracle runtime unavailable")

// Value is process-local oracle data. The transport receives Values only when
// the private program explicitly passes them as API arguments or receivers.
type Value struct {
	typ       types.Type
	data      reflect.Value
	reference uint64
	dynamic   types.Type // Concrete Go type when boxed in a local interface.
}

func (Value) String() string               { return "<private Go value>" }
func (Value) GoString() string             { return "<private Go value>" }
func (Value) MarshalJSON() ([]byte, error) { return nil, errPrivateSerialization }
func (v Value) Type() types.Type           { return v.typ }
func (v Value) Reference() (uint64, bool)  { return v.reference, v.reference != 0 }

// Data is for the trusted transport's explicit API argument encoding only.
func (v Value) Data() any {
	if !v.data.IsValid() {
		return nil
	}
	return v.data.Interface()
}

func nativeType(typ types.Type, depth int) (reflect.Type, error) {
	if typ == nil || depth > 32 {
		return nil, ErrRuntime
	}
	unalias := types.Unalias(typ)
	if named, ok := unalias.(*types.Named); ok && named.Obj().Pkg() != nil && named.Obj().Pkg().Path() == "time" {
		switch named.Obj().Name() {
		case "Time":
			return reflect.TypeOf(time.Time{}), nil
		case "Location":
			return reflect.TypeOf(time.Location{}), nil
		}
	}
	switch t := unalias.Underlying().(type) {
	case *types.Basic:
		if t.Info()&types.IsUntyped != 0 {
			if t.Kind() == types.UntypedNil {
				return nil, nil
			}
			return nativeType(types.Default(t), depth+1)
		}
		basic := map[types.BasicKind]reflect.Type{
			types.Bool: reflect.TypeOf(false), types.String: reflect.TypeOf(""), types.Int: reflect.TypeOf(int(0)),
			types.Int8: reflect.TypeOf(int8(0)), types.Int16: reflect.TypeOf(int16(0)), types.Int32: reflect.TypeOf(int32(0)), types.Int64: reflect.TypeOf(int64(0)),
			types.Uint: reflect.TypeOf(uint(0)), types.Uint8: reflect.TypeOf(uint8(0)), types.Uint16: reflect.TypeOf(uint16(0)), types.Uint32: reflect.TypeOf(uint32(0)), types.Uint64: reflect.TypeOf(uint64(0)),
			types.Float32: reflect.TypeOf(float32(0)), types.Float64: reflect.TypeOf(float64(0)),
		}
		if result := basic[t.Kind()]; result != nil {
			return result, nil
		}
	case *types.Slice:
		elem, err := nativeType(t.Elem(), depth+1)
		if err != nil {
			return nil, err
		}
		return reflect.SliceOf(elem), nil
	case *types.Array:
		if t.Len() > 4096 {
			return nil, ErrRuntime
		}
		elem, err := nativeType(t.Elem(), depth+1)
		if err != nil {
			return nil, err
		}
		return reflect.ArrayOf(int(t.Len()), elem), nil
	case *types.Map:
		key, err := nativeType(t.Key(), depth+1)
		if err != nil {
			return nil, err
		}
		elem, err := nativeType(t.Elem(), depth+1)
		if err != nil {
			return nil, err
		}
		return reflect.MapOf(key, elem), nil
	case *types.Pointer:
		elem, err := nativeType(t.Elem(), depth+1)
		if err != nil {
			return nil, err
		}
		return reflect.PointerTo(elem), nil
	case *types.Struct:
		fields := make([]reflect.StructField, t.NumFields())
		for i := range fields {
			field := t.Field(i)
			if !field.Exported() || field.Embedded() {
				return nil, ErrRuntime
			}
			ft, err := nativeType(field.Type(), depth+1)
			if err != nil {
				return nil, err
			}
			fields[i] = reflect.StructField{Name: field.Name(), Type: ft, Tag: reflect.StructTag(t.Tag(i))}
		}
		return reflect.StructOf(fields), nil
	case *types.Interface:
		return reflect.TypeOf((*any)(nil)).Elem(), nil
	}
	return nil, ErrRuntime
}

func NewValue(typ types.Type, data any) (Value, error) {
	native, err := nativeType(typ, 0)
	if err != nil {
		return Value{}, err
	}
	if data == nil {
		if native == nil {
			return Value{typ: typ}, nil
		}
		switch native.Kind() {
		case reflect.Slice, reflect.Map, reflect.Pointer, reflect.Interface:
			return Value{typ: typ, data: reflect.Zero(native)}, nil
		}
		return Value{}, ErrCandidate
	}
	value := reflect.ValueOf(data)
	if native == nil || native.Kind() == reflect.Interface || value.Type() != native {
		return Value{}, ErrCandidate
	}
	return Value{typ: typ, data: value}, nil
}

func NewReference(typ types.Type, id uint64) (Value, error) {
	if id == 0 || id > 4096 {
		return Value{}, ErrCandidate
	}
	switch types.Unalias(typ).Underlying().(type) {
	case *types.Interface, *types.Pointer:
		return Value{typ: typ, reference: id}, nil
	}
	return Value{}, ErrCandidate
}

func literal(value constant.Value, typ types.Type) (Value, error) {
	if basic, ok := typ.(*types.Basic); ok && basic.Info()&types.IsUntyped != 0 {
		typ = types.Default(typ)
	}
	native, err := nativeType(typ, 0)
	if err != nil || native == nil {
		return Value{}, ErrRuntime
	}
	result := reflect.New(native).Elem()
	switch result.Kind() {
	case reflect.Bool:
		result.SetBool(constant.BoolVal(value))
	case reflect.String:
		result.SetString(constant.StringVal(value))
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		integer, exact := constant.Int64Val(constant.ToInt(value))
		if !exact || result.OverflowInt(integer) {
			return Value{}, ErrRuntime
		}
		result.SetInt(integer)
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		integer, exact := constant.Uint64Val(constant.ToInt(value))
		if !exact || result.OverflowUint(integer) {
			return Value{}, ErrRuntime
		}
		result.SetUint(integer)
	case reflect.Float32:
		number, _ := constant.Float32Val(value)
		result.SetFloat(float64(number))
	case reflect.Float64:
		number, _ := constant.Float64Val(value)
		result.SetFloat(number)
	default:
		return Value{}, ErrRuntime
	}
	return Value{typ: typ, data: result}, nil
}

func nilValue(v Value) bool {
	if v.reference != 0 {
		return false
	}
	if !v.data.IsValid() {
		return true
	}
	switch v.data.Kind() {
	case reflect.Pointer, reflect.Interface, reflect.Map, reflect.Slice:
		return v.data.IsNil()
	}
	return false
}

func assign(value Value, typ types.Type) (Value, error) {
	if value.reference != 0 {
		if !types.AssignableTo(value.typ, typ) {
			return Value{}, ErrCandidate
		}
		value.typ = typ
		return value, nil
	}
	native, err := nativeType(typ, 0)
	if err != nil {
		return Value{}, err
	}
	if _, targetInterface := typ.Underlying().(*types.Interface); targetInterface {
		_, sourceInterface := value.typ.Underlying().(*types.Interface)
		if !value.data.IsValid() || (sourceInterface && nilValue(value)) {
			return NewValue(typ, nil)
		}
		boxed := reflect.New(native).Elem()
		boxed.Set(value.data)
		dynamic := value.typ
		if sourceInterface {
			dynamic = value.dynamic
		}
		return Value{typ: typ, data: boxed, dynamic: dynamic}, nil
	}
	if nilValue(value) {
		return NewValue(typ, nil)
	}
	if native == nil || !value.data.Type().ConvertibleTo(native) {
		return Value{}, ErrCandidate
	}
	copy := reflect.New(native).Elem()
	copy.Set(value.data.Convert(native))
	return Value{typ: typ, data: copy}, nil
}

func dynamicValue(value Value) (Value, error) {
	if value.reference != 0 {
		return Value{}, ErrRuntime
	}
	if !value.data.IsValid() {
		return value, nil
	}
	if _, interfaced := value.typ.Underlying().(*types.Interface); !interfaced {
		return value, nil
	}
	if value.data.IsNil() {
		return Value{}, nil
	}
	if value.dynamic == nil {
		return Value{}, ErrRuntime
	}
	return Value{typ: value.dynamic, data: value.data.Elem()}, nil
}

// ApplyArgumentSnapshot preserves observable mutations to passed slices/maps.
// It cannot replace caller headers or mutate unexposed oracle values. A transport
// must bind snapshots to the exact arguments it sent; no arbitrary object IDs.
func ApplyArgumentSnapshot(original, snapshot Value) error {
	if !types.Identical(original.typ, snapshot.typ) || original.reference != snapshot.reference {
		return ErrCandidate
	}
	if original.reference != 0 {
		return nil
	}
	if nilValue(original) || nilValue(snapshot) {
		if nilValue(original) != nilValue(snapshot) {
			return ErrCandidate
		}
		return nil
	}
	if original.data.Type() != snapshot.data.Type() {
		return ErrCandidate
	}
	switch original.data.Kind() {
	case reflect.Slice:
		if original.data.Len() != snapshot.data.Len() || original.data.Cap() != snapshot.data.Cap() {
			return ErrCandidate
		}
		reflect.Copy(original.data.Slice(0, original.data.Cap()), snapshot.data.Slice(0, snapshot.data.Cap()))
	case reflect.Map:
		var entries [][2]reflect.Value
		iterator := snapshot.data.MapRange()
		for iterator.Next() {
			entries = append(entries, [2]reflect.Value{iterator.Key(), iterator.Value()})
		}
		original.data.Clear()
		for _, entry := range entries {
			original.data.SetMapIndex(entry[0], entry[1])
		}
	}
	return nil
}
