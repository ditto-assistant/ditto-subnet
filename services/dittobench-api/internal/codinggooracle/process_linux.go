package codinggooracle

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"go/types"
	"math"
	"reflect"
	"strconv"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggobuild"
)

type wireValue struct {
	Kind      string       `json:"kind"`
	Type      string       `json:"type"`
	Text      string       `json:"text,omitempty"`
	Items     []wireValue  `json:"items,omitempty"`
	Length    int          `json:"length,omitempty"`
	Capacity  int          `json:"capacity,omitempty"`
	Reference uint64       `json:"reference,omitempty"`
	Instant   *wireInstant `json:"instant,omitempty"`
}
type wireInstant struct {
	Seconds     string `json:"seconds"`
	Nanoseconds int    `json:"nanoseconds"`
	Location    uint64 `json:"location"`
	Name        string `json:"name"`
	Offset      int    `json:"offset"`
	UTC         bool   `json:"utc"`
}
type wireRequest struct {
	ID        string      `json:"id"`
	Function  string      `json:"function,omitempty"`
	Reference uint64      `json:"reference,omitempty"`
	Method    string      `json:"method,omitempty"`
	Arguments []wireValue `json:"arguments"`
}
type wireResponse struct {
	ID        string      `json:"id"`
	Values    []wireValue `json:"values,omitempty"`
	Arguments []wireValue `json:"arguments,omitempty"`
	Failed    bool        `json:"failed,omitempty"`
}

type processClient struct {
	program    *codinggobuild.Program
	reader     *bufio.Reader
	ctx        context.Context
	cancel     context.CancelFunc
	functions  map[string]*types.Signature
	references map[uint64]types.Type
	locations  map[*time.Location]uint64
}

// ProcessFactory binds a frozen executable and independent API type information,
// not the private test suite. Each call creates a separately confined process.
func ProcessFactory(artifact codinggobuild.Artifact, functions map[string]*types.Signature, uid, gid uint32, timeout time.Duration) Factory {
	return func(parent context.Context) (Candidate, error) {
		if timeout <= 0 || timeout > 5*time.Minute {
			return nil, ErrRuntime
		}
		ctx, cancel := context.WithTimeout(parent, timeout)
		program, err := codinggobuild.Launch(ctx, artifact, uid, gid)
		if err != nil {
			cancel()
			if err == codinggobuild.ErrProgram {
				return nil, ErrCandidate
			}
			return nil, ErrRuntime
		}
		return &processClient{program: program, reader: bufio.NewReaderSize(program.Output(), 65536), ctx: ctx, cancel: cancel, functions: functions, references: map[uint64]types.Type{}, locations: map[*time.Location]uint64{}}, nil
	}
}

func wireType(typ types.Type) string {
	typ = types.Unalias(typ)
	if named, ok := typ.(*types.Named); ok {
		if named.Obj().Pkg() != nil {
			return named.Obj().Pkg().Path() + "." + named.Obj().Name()
		}
		return named.Obj().Name()
	}
	switch t := typ.(type) {
	case *types.Slice:
		return "[]" + wireType(t.Elem())
	case *types.Pointer:
		return "*" + wireType(t.Elem())
	}
	return types.TypeString(typ, func(pkg *types.Package) string { return pkg.Path() })
}

func (c *processClient) encode(value Value, depth int) (wireValue, error) {
	if depth > 32 || value.reference != 0 {
		return wireValue{}, ErrCandidate
	}
	wire := wireValue{Type: wireType(value.typ)}
	if nilValue(value) {
		wire.Kind = "nil"
		return wire, nil
	}
	if instant, ok := value.Data().(time.Time); ok {
		zone, offset := instant.Zone()
		location := instant.Location()
		id := uint64(0)
		if location != time.UTC {
			if offset <= -86400 || offset >= 86400 {
				return wireValue{}, ErrRuntime
			}
			id = c.locations[location]
			if id == 0 {
				id = uint64(len(c.locations) + 1)
				if id > 4096 {
					return wireValue{}, ErrRuntime
				}
				c.locations[location] = id
			}
		}
		wire.Kind = "time"
		wire.Instant = &wireInstant{Seconds: strconv.FormatInt(instant.Unix(), 10), Nanoseconds: instant.Nanosecond(), Location: id, Name: zone, Offset: offset, UTC: location == time.UTC}
		return wire, nil
	}
	wire.Kind = value.data.Kind().String()
	switch value.data.Kind() {
	case reflect.Bool:
		wire.Text = strconv.FormatBool(value.data.Bool())
	case reflect.String:
		wire.Text = value.data.String()
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		wire.Text = strconv.FormatInt(value.data.Int(), 10)
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		wire.Text = strconv.FormatUint(value.data.Uint(), 10)
	case reflect.Float32:
		wire.Text = strconv.FormatUint(uint64(math.Float32bits(float32(value.data.Float()))), 16)
	case reflect.Float64:
		wire.Text = strconv.FormatUint(math.Float64bits(value.data.Float()), 16)
	case reflect.Slice:
		if value.data.Cap() > 4096 {
			return wireValue{}, ErrCandidate
		}
		wire.Length = value.data.Len()
		wire.Capacity = value.data.Cap()
		typ := value.typ.Underlying().(*types.Slice)
		all := value.data.Slice(0, value.data.Cap())
		for i := 0; i < all.Len(); i++ {
			item, err := c.encode(Value{typ: typ.Elem(), data: all.Index(i)}, depth+1)
			if err != nil {
				return wireValue{}, err
			}
			wire.Items = append(wire.Items, item)
		}
	default:
		return wireValue{}, ErrRuntime
	}
	return wire, nil
}

func (c *processClient) decode(wire wireValue, typ types.Type, depth int) (Value, error) {
	if depth > 32 || wire.Type != wireType(typ) {
		return Value{}, ErrCandidate
	}
	if wire.Kind == "reference" {
		if wire.Text != "" || wire.Instant != nil || len(wire.Items) != 0 || wire.Length != 0 || wire.Capacity != 0 {
			return Value{}, ErrCandidate
		}
		if previous, ok := c.references[wire.Reference]; ok && !types.Identical(previous, typ) {
			return Value{}, ErrCandidate
		}
		value, err := NewReference(typ, wire.Reference)
		if err == nil {
			c.references[wire.Reference] = typ
		}
		return value, err
	}
	if wire.Reference != 0 || wire.Instant != nil {
		return Value{}, ErrCandidate
	}
	if wire.Kind == "nil" {
		if wire.Text != "" || len(wire.Items) != 0 || wire.Length != 0 || wire.Capacity != 0 {
			return Value{}, ErrCandidate
		}
		return NewValue(typ, nil)
	}
	native, err := nativeType(typ, 0)
	if err != nil {
		return Value{}, err
	}
	if native == nil || wire.Kind != native.Kind().String() {
		return Value{}, ErrCandidate
	}
	data := reflect.New(native).Elem()
	if native.Kind() != reflect.Slice && (len(wire.Items) != 0 || wire.Length != 0 || wire.Capacity != 0) {
		return Value{}, ErrCandidate
	}
	switch native.Kind() {
	case reflect.Bool:
		if wire.Text != "true" && wire.Text != "false" {
			return Value{}, ErrCandidate
		}
		data.SetBool(wire.Text == "true")
	case reflect.String:
		data.SetString(wire.Text)
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		number, err := strconv.ParseInt(wire.Text, 10, native.Bits())
		if err != nil || strconv.FormatInt(number, 10) != wire.Text {
			return Value{}, ErrCandidate
		}
		data.SetInt(number)
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		number, err := strconv.ParseUint(wire.Text, 10, native.Bits())
		if err != nil || strconv.FormatUint(number, 10) != wire.Text {
			return Value{}, ErrCandidate
		}
		data.SetUint(number)
	case reflect.Float32:
		bits, err := strconv.ParseUint(wire.Text, 16, 32)
		if err != nil || strconv.FormatUint(bits, 16) != wire.Text {
			return Value{}, ErrCandidate
		}
		data.SetFloat(float64(math.Float32frombits(uint32(bits))))
	case reflect.Float64:
		bits, err := strconv.ParseUint(wire.Text, 16, 64)
		if err != nil || strconv.FormatUint(bits, 16) != wire.Text {
			return Value{}, ErrCandidate
		}
		data.SetFloat(math.Float64frombits(bits))
	case reflect.Slice:
		if wire.Text != "" || wire.Length < 0 || wire.Capacity < wire.Length || wire.Capacity > 4096 || len(wire.Items) != wire.Capacity {
			return Value{}, ErrCandidate
		}
		data = reflect.MakeSlice(native, wire.Capacity, wire.Capacity)
		element := typ.Underlying().(*types.Slice).Elem()
		for i, item := range wire.Items {
			decoded, err := c.decode(item, element, depth+1)
			if err != nil || decoded.reference != 0 {
				return Value{}, ErrCandidate
			}
			data.Index(i).Set(decoded.data)
		}
		data = data.Slice(0, wire.Length)
	default:
		return Value{}, ErrRuntime
	}
	return Value{typ: typ, data: data}, nil
}

func (c *processClient) invoke(request wireRequest, arguments []Value, signature *types.Signature) ([]Value, error) {
	if c.ctx.Err() != nil {
		return nil, ErrCandidate
	}
	if signature == nil || signature.Variadic() || signature.Params().Len() != len(arguments) {
		return nil, ErrRuntime
	}
	identity := make([]byte, 16)
	if _, err := rand.Read(identity); err != nil {
		return nil, ErrRuntime
	}
	request.ID = hex.EncodeToString(identity)
	request.Arguments = []wireValue{}
	// In particular, Go leaves nil literals untyped in types.Info. Bind every
	// argument to the function parameter before encoding or applying effects.
	bound := make([]Value, len(arguments))
	for i, argument := range arguments {
		value, err := assign(argument, signature.Params().At(i).Type())
		if err != nil {
			return nil, err
		}
		bound[i] = value
	}
	arguments = bound
	for _, argument := range arguments {
		encoded, err := c.encode(argument, 0)
		if err != nil {
			return nil, err
		}
		request.Arguments = append(request.Arguments, encoded)
	}
	encoded, err := json.Marshal(request)
	if err != nil || len(encoded)+1 > 65536 {
		return nil, ErrCandidate
	}
	encoded = append(encoded, '\n')
	deadline, _ := c.ctx.Deadline()
	writer, ok := c.program.Input().(interface{ SetWriteDeadline(time.Time) error })
	if !ok || writer.SetWriteDeadline(deadline) != nil || c.program.Output().SetReadDeadline(deadline) != nil {
		return nil, ErrRuntime
	}
	if _, err := c.program.Input().Write(encoded); err != nil {
		return nil, ErrCandidate
	}
	raw, err := c.reader.ReadSlice('\n')
	if err != nil || len(raw) > 65536 || c.reader.Buffered() != 0 {
		return nil, ErrCandidate
	}
	var response wireResponse
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&response) != nil || response.ID != request.ID {
		return nil, ErrCandidate
	}
	canonical, err := json.Marshal(response)
	if err != nil || !bytes.Equal(append(canonical, '\n'), raw) {
		return nil, ErrCandidate
	}
	if response.Failed {
		return nil, fmt.Errorf("API invocation failed: %w", ErrCandidate)
	}
	if len(response.Values) != signature.Results().Len() || len(response.Arguments) != len(arguments) {
		return nil, fmt.Errorf("API result arity rejected: %w", ErrCandidate)
	}
	values := make([]Value, len(response.Values))
	for i, wire := range response.Values {
		value, err := c.decode(wire, signature.Results().At(i).Type(), 0)
		if err != nil {
			return nil, fmt.Errorf("API result value rejected: %w", err)
		}
		values[i] = value
	}
	for i, wire := range response.Arguments {
		if wire.Kind == "unchanged" {
			if wire.Type != wireType(arguments[i].typ) || wire.Text != "" || wire.Reference != 0 || wire.Instant != nil || len(wire.Items) != 0 || wire.Length != 0 || wire.Capacity != 0 || arguments[i].data.Kind() == reflect.Slice {
				return nil, fmt.Errorf("API unchanged argument rejected: %w", ErrCandidate)
			}
			continue
		}
		value, err := c.decode(wire, arguments[i].typ, 0)
		if err != nil || ApplyArgumentSnapshot(arguments[i], value) != nil {
			return nil, fmt.Errorf("API argument effect rejected: %w", ErrCandidate)
		}
	}
	return values, nil
}

func (c *processClient) Call(_ context.Context, name string, arguments []Value) ([]Value, error) {
	signature, ok := c.functions[name]
	if !ok {
		return nil, ErrRuntime
	}
	return c.invoke(wireRequest{Function: name}, arguments, signature)
}
func (c *processClient) Method(_ context.Context, receiver Value, name string, arguments []Value) ([]Value, error) {
	typ, ok := c.references[receiver.reference]
	if !ok || !types.Identical(typ, receiver.typ) {
		return nil, ErrCandidate
	}
	object, _, _ := types.LookupFieldOrMethod(typ, true, nil, name)
	function, ok := object.(*types.Func)
	if !ok {
		return nil, ErrRuntime
	}
	return c.invoke(wireRequest{Reference: receiver.reference, Method: name}, arguments, function.Signature())
}
func (c *processClient) Close() error {
	defer c.cancel()
	if c.program.Close() != nil {
		return ErrRuntime
	}
	return nil
}
