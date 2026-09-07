package codinggooracle

import (
	"bytes"
	"context"
	"errors"
	"go/ast"
	"go/token"
	"go/types"
	"reflect"
	"time"
)

// Candidate never receives private test AST, names, assertions or expected totals.
// All results are proposed API values. Implementations own transport limits and
// kernel launch proof, and Close must prove whole-candidate termination.
type Candidate interface {
	Call(context.Context, string, []Value) ([]Value, error)
	Method(context.Context, Value, string, []Value) ([]Value, error)
	Close() error
}
type Factory func(context.Context) (Candidate, error)
type Result struct {
	Passed    int
	Total     int
	Completed bool
}

type evaluator struct {
	suite       *Suite
	client      Candidate
	ctx         context.Context
	locals      map[types.Object]Value
	steps       int
	failed      bool
	helperDepth int
}
type flow struct {
	returned bool
	fatal    bool
	values   []Value
}

func Run(ctx context.Context, suite *Suite, factory Factory) (result Result, err error) {
	if suite == nil || factory == nil {
		return Result{}, ErrRuntime
	}
	result.Total = len(suite.tests)
	for _, test := range suite.tests {
		if ctx.Err() != nil {
			return result, ctx.Err()
		}
		client, startErr := factory(ctx)
		if startErr != nil {
			if client != nil && closeCandidate(client) != nil {
				return result, ErrRuntime
			}
			if errors.Is(startErr, ErrCandidate) {
				continue
			}
			return result, ErrRuntime
		}
		if client == nil {
			return result, ErrRuntime
		}
		e := evaluator{suite: suite, client: client, ctx: ctx, locals: map[types.Object]Value{}}
		runErr, closeErr := e.runAndClose(test.Body)
		if closeErr != nil {
			return result, ErrRuntime
		}
		if runErr != nil && runErr != ErrCandidate {
			return result, runErr
		}
		if runErr == nil && !e.failed {
			result.Passed++
		}
	}
	result.Completed = true
	return result, nil
}

func (e *evaluator) runAndClose(body *ast.BlockStmt) (runErr, closeErr error) {
	defer func() {
		if recover() != nil {
			runErr = ErrRuntime
		}
		closeErr = closeCandidate(e.client)
	}()
	_, runErr = e.block(body)
	return runErr, nil
}

func closeCandidate(client Candidate) (err error) {
	defer func() {
		if recover() != nil {
			err = ErrRuntime
		}
	}()
	return client.Close()
}

func callCandidate(invoke func() ([]Value, error)) (values []Value, err error) {
	defer func() {
		if recover() != nil {
			values = nil
			err = ErrRuntime
		}
	}()
	return invoke()
}

func (e *evaluator) step() error {
	e.steps++
	if e.ctx.Err() != nil {
		return e.ctx.Err()
	}
	if e.steps > 100000 {
		return ErrRuntime
	}
	return nil
}
func (e *evaluator) one(expr ast.Expr) (Value, error) {
	values, err := e.expression(expr)
	if err != nil {
		return Value{}, err
	}
	if len(values) != 1 {
		return Value{}, ErrRuntime
	}
	return values[0], nil
}
func (e *evaluator) list(expressions []ast.Expr) ([]Value, error) {
	var values []Value
	for _, expr := range expressions {
		items, err := e.expression(expr)
		if err != nil {
			return nil, err
		}
		values = append(values, items...)
	}
	return values, nil
}
func single(v Value, err error) ([]Value, error) {
	if err != nil {
		return nil, err
	}
	return []Value{v}, nil
}

func (e *evaluator) expression(expr ast.Expr) (values []Value, err error) {
	if err = e.step(); err != nil {
		return nil, err
	}
	// Reflection panics (bad indexing, nil dereference, invalid conversion) are
	// candidate/test failures, never private diagnostics or successful reports.
	defer func() {
		if recover() != nil {
			values = nil
			err = ErrCandidate
		}
	}()
	info := e.suite.info
	if constant := info.Types[expr]; constant.Value != nil {
		return single(literal(constant.Value, constant.Type))
	}
	switch node := expr.(type) {
	case *ast.Ident:
		object := info.Uses[node]
		if _, ok := object.(*types.Nil); ok {
			return single(NewValue(info.TypeOf(node), nil))
		}
		value, ok := e.locals[object]
		if !ok {
			return nil, ErrRuntime
		}
		return []Value{value}, nil
	case *ast.ParenExpr:
		return e.expression(node.X)
	case *ast.UnaryExpr:
		value, err := e.one(node.X)
		if err != nil {
			return nil, err
		}
		if value.reference != 0 {
			return nil, ErrCandidate
		}
		if node.Op == token.NOT {
			return single(NewValue(info.TypeOf(node), !value.data.Bool()))
		}
		result := reflect.New(value.data.Type()).Elem()
		result.Set(value.data)
		switch result.Kind() {
		case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
			if node.Op == token.SUB {
				result.SetInt(-result.Int())
			} else if node.Op == token.XOR {
				result.SetInt(^result.Int())
			}
		case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
			if node.Op == token.SUB {
				result.SetUint(-result.Uint())
			} else if node.Op == token.XOR {
				result.SetUint(^result.Uint())
			}
		case reflect.Float32, reflect.Float64:
			if node.Op == token.SUB {
				result.SetFloat(-result.Float())
			}
		default:
			return nil, ErrRuntime
		}
		return []Value{{typ: info.TypeOf(node), data: result}}, nil
	case *ast.BinaryExpr:
		left, err := e.one(node.X)
		if err != nil {
			return nil, err
		}
		if node.Op == token.LAND && !left.data.Bool() {
			return single(NewValue(info.TypeOf(node), false))
		}
		if node.Op == token.LOR && left.data.Bool() {
			return single(NewValue(info.TypeOf(node), true))
		}
		right, err := e.one(node.Y)
		if err != nil {
			return nil, err
		}
		return single(binary(node.Op, left, right, info.TypeOf(node)))
	case *ast.IndexExpr:
		base, err := e.one(node.X)
		if err != nil {
			return nil, err
		}
		index, err := e.one(node.Index)
		if err != nil {
			return nil, err
		}
		if base.reference != 0 {
			return nil, ErrRuntime
		}
		if base.data.Kind() == reflect.Map {
			value := base.data.MapIndex(index.data)
			if !value.IsValid() {
				value = reflect.Zero(base.data.Type().Elem())
			}
			return []Value{{typ: info.TypeOf(node), data: value}}, nil
		}
		n, err := indexNumber(index)
		if err != nil {
			return nil, err
		}
		if n < 0 || n >= int64(base.data.Len()) {
			return nil, ErrCandidate
		}
		if base.data.Kind() == reflect.String {
			return single(NewValue(info.TypeOf(node), base.data.String()[n]))
		}
		return []Value{{typ: info.TypeOf(node), data: base.data.Index(int(n))}}, nil
	case *ast.SliceExpr:
		base, err := e.one(node.X)
		if err != nil {
			return nil, err
		}
		if base.reference != 0 {
			return nil, ErrRuntime
		}
		low, high, max := 0, base.data.Len(), -1
		for _, bound := range []struct {
			node   ast.Expr
			target *int
		}{{node.Low, &low}, {node.High, &high}, {node.Max, &max}} {
			if bound.node != nil {
				v, err := e.one(bound.node)
				if err != nil {
					return nil, err
				}
				index, err := indexNumber(v)
				if err != nil {
					return nil, err
				}
				*bound.target = int(index)
			}
		}
		if max >= 0 {
			return []Value{{typ: info.TypeOf(node), data: base.data.Slice3(low, high, max)}}, nil
		}
		return []Value{{typ: info.TypeOf(node), data: base.data.Slice(low, high)}}, nil
	case *ast.CompositeLit:
		return single(e.composite(node))
	case *ast.SelectorExpr:
		if selection := info.Selections[node]; selection != nil {
			base, err := e.one(node.X)
			if err != nil {
				return nil, err
			}
			if base.reference != 0 {
				return nil, ErrRuntime
			}
			data := base.data
			if data.Kind() == reflect.Pointer {
				data = data.Elem()
			}
			for _, index := range selection.Index() {
				data = data.Field(index)
			}
			return []Value{{typ: info.TypeOf(node), data: data}}, nil
		}
		object := info.Uses[node.Sel]
		if object != nil && object.Pkg() != nil && object.Pkg().Path() == "time" && object.Name() == "UTC" {
			return single(NewValue(info.TypeOf(node), time.UTC))
		}
		return nil, ErrRuntime
	case *ast.CallExpr:
		return e.call(node)
	}
	return nil, ErrRuntime
}

func (e *evaluator) composite(node *ast.CompositeLit) (Value, error) {
	typ := e.suite.info.TypeOf(node)
	native, err := nativeType(typ, 0)
	if err != nil {
		return Value{}, err
	}
	var data reflect.Value
	switch native.Kind() {
	case reflect.Slice:
		length, next := 0, 0
		for _, element := range node.Elts {
			if pair, ok := element.(*ast.KeyValueExpr); ok {
				index, err := e.one(pair.Key)
				if err != nil {
					return Value{}, err
				}
				next = int(index.data.Int())
			}
			if next < 0 || next >= 4096 {
				return Value{}, ErrRuntime
			}
			if next+1 > length {
				length = next + 1
			}
			next++
		}
		data = reflect.MakeSlice(native, length, length)
	case reflect.Map:
		data = reflect.MakeMap(native)
	default:
		data = reflect.New(native).Elem()
	}
	next := 0
	for _, element := range node.Elts {
		valueExpr := element
		var key ast.Expr
		if pair, ok := element.(*ast.KeyValueExpr); ok {
			key = pair.Key
			valueExpr = pair.Value
		}
		value, err := e.one(valueExpr)
		if err != nil {
			return Value{}, err
		}
		if value.reference != 0 {
			return Value{}, ErrRuntime
		}
		switch data.Kind() {
		case reflect.Map:
			k, err := e.one(key)
			if err != nil {
				return Value{}, err
			}
			data.SetMapIndex(k.data, value.data)
		case reflect.Struct:
			index := next
			if key != nil {
				field, ok := data.Type().FieldByName(key.(*ast.Ident).Name)
				if !ok {
					return Value{}, ErrRuntime
				}
				index = field.Index[0]
			}
			data.Field(index).Set(value.data)
			next++
		case reflect.Array, reflect.Slice:
			if key != nil {
				k, err := e.one(key)
				if err != nil {
					return Value{}, err
				}
				next = int(k.data.Int())
			}
			data.Index(next).Set(value.data)
			next++
		default:
			return Value{}, ErrRuntime
		}
	}
	return Value{typ: typ, data: data}, nil
}

func (e *evaluator) call(node *ast.CallExpr) ([]Value, error) {
	info := e.suite.info
	// Go evaluates a method receiver before its arguments.
	var receiver Value
	var selection *types.Selection
	if target, ok := node.Fun.(*ast.SelectorExpr); ok {
		selection = info.Selections[target]
		if selection != nil {
			var err error
			receiver, err = e.one(target.X)
			if err != nil {
				return nil, err
			}
		}
	}
	arguments, err := e.list(node.Args)
	if err != nil {
		return nil, err
	}
	if info.Types[node.Fun].IsType() {
		if len(arguments) != 1 {
			return nil, ErrRuntime
		}
		return single(assign(arguments[0], info.TypeOf(node)))
	}
	if selection != nil {
		method := selection.Obj().(*types.Func)
		if trustedTimeMethod(selection) && receiver.reference == 0 {
			data := receiver.data
			if data.Kind() == reflect.Pointer {
				if data.IsNil() {
					return nil, ErrCandidate
				}
				data = data.Elem()
			}
			instant := data.Interface().(time.Time)
			switch method.Name() {
			case "Add":
				return single(NewValue(info.TypeOf(node), instant.Add(time.Duration(arguments[0].data.Int()))))
			case "In":
				return single(NewValue(info.TypeOf(node), instant.In(arguments[0].data.Interface().(*time.Location))))
			}
		}
		values, err := callCandidate(func() ([]Value, error) { return e.client.Method(e.ctx, receiver, method.Name(), arguments) })
		return e.results(node, values, err)
	}
	var object types.Object
	switch target := node.Fun.(type) {
	case *ast.Ident:
		object = info.Uses[target]
	case *ast.SelectorExpr:
		object = info.Uses[target.Sel]
	default:
		return nil, ErrRuntime
	}
	if e.suite.api[object] {
		values, err := callCandidate(func() ([]Value, error) { return e.client.Call(e.ctx, object.Name(), arguments) })
		return e.results(node, values, err)
	}
	if function, ok := object.(*types.Func); ok {
		if helper := e.suite.helpers[function]; helper != nil {
			return e.helper(function, helper, arguments)
		}
		if function.Pkg() != nil {
			switch function.Pkg().Path() + "." + function.Name() {
			case "reflect.DeepEqual":
				left, err := dynamicValue(arguments[0])
				if err != nil {
					return nil, err
				}
				right, err := dynamicValue(arguments[1])
				if err != nil {
					return nil, err
				}
				same := !left.data.IsValid() && !right.data.IsValid()
				if left.data.IsValid() && right.data.IsValid() {
					same = types.Identical(left.typ, right.typ) && reflect.DeepEqual(left.Data(), right.Data())
				}
				return single(NewValue(info.TypeOf(node), same))
			case "bytes.Equal":
				return single(NewValue(info.TypeOf(node), bytes.Equal(arguments[0].Data().([]byte), arguments[1].Data().([]byte))))
			case "time.FixedZone":
				return single(NewValue(info.TypeOf(node), time.FixedZone(arguments[0].data.String(), int(arguments[1].data.Int()))))
			case "time.Date":
				return single(NewValue(info.TypeOf(node), time.Date(int(arguments[0].data.Int()), time.Month(arguments[1].data.Int()), int(arguments[2].data.Int()), int(arguments[3].data.Int()), int(arguments[4].data.Int()), int(arguments[5].data.Int()), int(arguments[6].data.Int()), arguments[7].Data().(*time.Location))))
			}
		}
	}
	if builtin, ok := object.(*types.Builtin); ok {
		if arguments[0].reference != 0 {
			return nil, ErrRuntime
		}
		switch builtin.Name() {
		case "len":
			return single(NewValue(info.TypeOf(node), arguments[0].data.Len()))
		case "cap":
			return single(NewValue(info.TypeOf(node), arguments[0].data.Cap()))
		}
	}
	return nil, ErrRuntime
}

func (e *evaluator) results(node *ast.CallExpr, values []Value, err error) ([]Value, error) {
	if err != nil {
		if errors.Is(err, ErrCandidate) {
			return nil, ErrCandidate
		}
		return nil, ErrRuntime
	}
	typ := e.suite.info.TypeOf(node)
	var expected []types.Type
	if tuple, ok := typ.(*types.Tuple); ok {
		for i := 0; i < tuple.Len(); i++ {
			expected = append(expected, tuple.At(i).Type())
		}
	} else if typ != nil {
		expected = []types.Type{typ}
	}
	if len(values) != len(expected) {
		return nil, ErrCandidate
	}
	for i := range values {
		if !types.Identical(values[i].typ, expected[i]) {
			return nil, ErrCandidate
		}
	}
	return values, nil
}

func (e *evaluator) helper(function *types.Func, declaration *ast.FuncDecl, arguments []Value) ([]Value, error) {
	if e.helperDepth >= 32 {
		return nil, ErrRuntime
	}
	signature := function.Signature()
	if signature.Params().Len() != len(arguments) {
		return nil, ErrRuntime
	}
	previous := e.locals
	e.locals = map[types.Object]Value{}
	e.helperDepth++
	defer func() { e.locals = previous; e.helperDepth-- }()
	for i := range arguments {
		value, err := assign(arguments[i], signature.Params().At(i).Type())
		if err != nil {
			return nil, err
		}
		e.locals[signature.Params().At(i)] = value
	}
	for i := 0; i < signature.Results().Len(); i++ {
		object := signature.Results().At(i)
		if object.Name() == "" || object.Name() == "_" {
			continue
		}
		native, err := nativeType(object.Type(), 0)
		if err != nil {
			return nil, err
		}
		e.locals[object] = Value{typ: object.Type(), data: reflect.New(native).Elem()}
	}
	result, err := e.block(declaration.Body)
	if err != nil {
		return nil, err
	}
	if !result.returned && signature.Results().Len() != 0 {
		return nil, ErrRuntime
	}
	if result.returned && len(result.values) == 0 && signature.Results().Len() > 0 {
		for i := 0; i < signature.Results().Len(); i++ {
			value, ok := e.locals[signature.Results().At(i)]
			if !ok {
				return nil, ErrRuntime
			}
			result.values = append(result.values, value)
		}
	}
	if len(result.values) != signature.Results().Len() {
		return nil, ErrRuntime
	}
	for i := range result.values {
		value, err := assign(result.values[i], signature.Results().At(i).Type())
		if err != nil {
			return nil, err
		}
		result.values[i] = value
	}
	return result.values, nil
}

func (e *evaluator) block(block *ast.BlockStmt) (flow, error) {
	for _, statement := range block.List {
		result, err := e.statement(statement)
		if err != nil || result.returned || result.fatal {
			return result, err
		}
	}
	return flow{}, nil
}
func (e *evaluator) statement(statement ast.Stmt) (result flow, err error) {
	if err = e.step(); err != nil {
		return flow{}, err
	}
	defer func() {
		if recover() != nil {
			result = flow{}
			err = ErrCandidate
		}
	}()
	info := e.suite.info
	switch node := statement.(type) {
	case *ast.AssignStmt:
		values, err := e.list(node.Rhs)
		if err != nil {
			return flow{}, err
		}
		if len(values) != len(node.Lhs) {
			return flow{}, ErrRuntime
		}
		for i, raw := range node.Lhs {
			id := raw.(*ast.Ident)
			if id.Name == "_" {
				continue
			}
			object := info.ObjectOf(id)
			value, err := assign(values[i], object.Type())
			if err != nil {
				return flow{}, err
			}
			e.locals[object] = value
		}
	case *ast.DeclStmt:
		for _, raw := range node.Decl.(*ast.GenDecl).Specs {
			spec := raw.(*ast.ValueSpec)
			values, err := e.list(spec.Values)
			if err != nil {
				return flow{}, err
			}
			for i, id := range spec.Names {
				if id.Name == "_" {
					continue
				}
				object := info.Defs[id]
				var value Value
				if c, ok := object.(*types.Const); ok {
					value, err = literal(c.Val(), c.Type())
				} else if len(values) == 0 {
					native, nativeErr := nativeType(object.Type(), 0)
					if nativeErr != nil {
						return flow{}, nativeErr
					}
					value = Value{typ: object.Type(), data: reflect.New(native).Elem()}
				} else {
					value, err = assign(values[i], object.Type())
				}
				if err != nil {
					return flow{}, err
				}
				e.locals[object] = value
			}
		}
	case *ast.ExprStmt:
		_, err := e.expression(node.X)
		return flow{}, err
	case *ast.BlockStmt:
		return e.block(node)
	case *ast.ReturnStmt:
		values, err := e.list(node.Results)
		return flow{returned: true, values: values}, err
	case *ast.IfStmt:
		if node.Init != nil {
			if _, err := e.statement(node.Init); err != nil {
				return flow{}, err
			}
		}
		condition, err := e.one(node.Cond)
		if err != nil {
			return flow{}, err
		}
		if !condition.data.Bool() {
			if node.Else != nil {
				return e.statement(node.Else)
			}
			return flow{}, nil
		}
		if e.helperDepth > 0 {
			return e.block(node.Body)
		}
		call := node.Body.List[0].(*ast.ExprStmt).X.(*ast.CallExpr)
		method := call.Fun.(*ast.SelectorExpr).Sel.Name
		e.failed = true
		return flow{fatal: method == "Fatal" || method == "Fatalf" || method == "FailNow"}, nil
	case *ast.RangeStmt:
		value, err := e.one(node.X)
		if err != nil {
			return flow{}, err
		}
		if value.reference != 0 {
			return flow{}, ErrRuntime
		}
		iterate := func(key, item reflect.Value) (flow, error) {
			if err := e.step(); err != nil {
				return flow{}, err
			}
			for _, binding := range []struct {
				expr ast.Expr
				data reflect.Value
			}{{node.Key, key}, {node.Value, item}} {
				if binding.expr == nil {
					continue
				}
				id := binding.expr.(*ast.Ident)
				if id.Name == "_" {
					continue
				}
				object := info.Defs[id]
				value, err := assign(Value{typ: object.Type(), data: binding.data}, object.Type())
				if err != nil {
					return flow{}, err
				}
				e.locals[object] = value
			}
			return e.block(node.Body)
		}
		switch value.data.Kind() {
		case reflect.String:
			for index, r := range value.data.String() {
				result, err := iterate(reflect.ValueOf(index), reflect.ValueOf(r))
				if err != nil || result.returned {
					return result, err
				}
			}
		case reflect.Map:
			iterator := value.data.MapRange()
			for iterator.Next() {
				result, err := iterate(iterator.Key(), iterator.Value())
				if err != nil || result.returned {
					return result, err
				}
			}
		default:
			for i := 0; i < value.data.Len(); i++ {
				result, err := iterate(reflect.ValueOf(i), value.data.Index(i))
				if err != nil || result.returned {
					return result, err
				}
			}
		}
	default:
		return flow{}, ErrRuntime
	}
	return flow{}, nil
}

func binary(operator token.Token, left, right Value, resultType types.Type) (Value, error) {
	if operator == token.EQL || operator == token.NEQ {
		// Interfaces compare their dynamic types/values. A typed nil pointer
		// boxed in an interface is not the nil interface.
		leftInterface := false
		rightInterface := false
		if left.typ != nil {
			_, leftInterface = left.typ.Underlying().(*types.Interface)
		}
		if right.typ != nil {
			_, rightInterface = right.typ.Underlying().(*types.Interface)
		}
		if (leftInterface || rightInterface) && left.reference == 0 && right.reference == 0 {
			l, err := dynamicValue(left)
			if err != nil {
				return Value{}, err
			}
			r, err := dynamicValue(right)
			if err != nil {
				return Value{}, err
			}
			equal := !l.data.IsValid() && !r.data.IsValid()
			if l.data.IsValid() && r.data.IsValid() {
				if !l.data.Comparable() || !r.data.Comparable() {
					return Value{}, ErrCandidate
				}
				equal = types.Identical(l.typ, r.typ) && l.data.Equal(r.data)
			}
			if operator == token.NEQ {
				equal = !equal
			}
			return NewValue(resultType, equal)
		}
		var equal bool
		if !left.data.IsValid() || !right.data.IsValid() || left.reference != 0 || right.reference != 0 ||
			((left.data.Kind() == reflect.Slice || left.data.Kind() == reflect.Map || right.data.Kind() == reflect.Slice || right.data.Kind() == reflect.Map) && (nilValue(left) || nilValue(right))) {
			if left.reference != 0 && right.reference != 0 {
				return Value{}, ErrRuntime
			}
			equal = nilValue(left) && nilValue(right)
		} else {
			if !left.data.Comparable() || !right.data.Comparable() {
				return Value{}, ErrRuntime
			}
			equal = left.data.Equal(right.data)
		}
		if operator == token.NEQ {
			equal = !equal
		}
		return NewValue(resultType, equal)
	}
	if left.reference != 0 || right.reference != 0 {
		return Value{}, ErrCandidate
	}
	if operator == token.LAND {
		return NewValue(resultType, left.data.Bool() && right.data.Bool())
	}
	if operator == token.LOR {
		return NewValue(resultType, left.data.Bool() || right.data.Bool())
	}
	native, err := nativeType(resultType, 0)
	if err != nil {
		return Value{}, err
	}
	result := reflect.New(native).Elem()
	if operator == token.SHL || operator == token.SHR {
		count, err := shiftCount(right)
		if err != nil {
			return Value{}, err
		}
		switch left.data.Kind() {
		case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
			if operator == token.SHL {
				result.SetInt(left.data.Int() << count)
			} else {
				result.SetInt(left.data.Int() >> count)
			}
		case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
			if operator == token.SHL {
				result.SetUint(left.data.Uint() << count)
			} else {
				result.SetUint(left.data.Uint() >> count)
			}
		default:
			return Value{}, ErrRuntime
		}
		return Value{typ: resultType, data: result}, nil
	}
	compare := func(less, equal bool) (Value, error) {
		switch operator {
		case token.LSS:
			return NewValue(resultType, less)
		case token.LEQ:
			return NewValue(resultType, less || equal)
		case token.GTR:
			return NewValue(resultType, !less && !equal)
		case token.GEQ:
			return NewValue(resultType, !less)
		}
		return Value{}, ErrRuntime
	}
	switch left.data.Kind() {
	case reflect.String:
		a, b := left.data.String(), right.data.String()
		if operator == token.ADD {
			result.SetString(a + b)
		} else {
			return compare(a < b, a == b)
		}
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		a, b := left.data.Int(), right.data.Int()
		switch operator {
		case token.ADD:
			result.SetInt(a + b)
		case token.SUB:
			result.SetInt(a - b)
		case token.MUL:
			result.SetInt(a * b)
		case token.QUO:
			if b == 0 {
				return Value{}, ErrCandidate
			}
			result.SetInt(a / b)
		case token.REM:
			if b == 0 {
				return Value{}, ErrCandidate
			}
			result.SetInt(a % b)
		case token.AND:
			result.SetInt(a & b)
		case token.OR:
			result.SetInt(a | b)
		case token.XOR:
			result.SetInt(a ^ b)
		case token.AND_NOT:
			result.SetInt(a &^ b)
		default:
			return compare(a < b, a == b)
		}
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		a, b := left.data.Uint(), right.data.Uint()
		switch operator {
		case token.ADD:
			result.SetUint(a + b)
		case token.SUB:
			result.SetUint(a - b)
		case token.MUL:
			result.SetUint(a * b)
		case token.QUO:
			if b == 0 {
				return Value{}, ErrCandidate
			}
			result.SetUint(a / b)
		case token.REM:
			if b == 0 {
				return Value{}, ErrCandidate
			}
			result.SetUint(a % b)
		case token.AND:
			result.SetUint(a & b)
		case token.OR:
			result.SetUint(a | b)
		case token.XOR:
			result.SetUint(a ^ b)
		case token.AND_NOT:
			result.SetUint(a &^ b)
		default:
			return compare(a < b, a == b)
		}
	case reflect.Float32, reflect.Float64:
		a, b := left.data.Float(), right.data.Float()
		// Ordered comparisons with NaN are all false; they cannot be derived by
		// negating less/equal. Preserve float32 arithmetic rounding explicitly.
		switch operator {
		case token.LSS:
			return NewValue(resultType, a < b)
		case token.LEQ:
			return NewValue(resultType, a <= b)
		case token.GTR:
			return NewValue(resultType, a > b)
		case token.GEQ:
			return NewValue(resultType, a >= b)
		}
		if left.data.Kind() == reflect.Float32 {
			x, y := float32(a), float32(b)
			switch operator {
			case token.ADD:
				result.SetFloat(float64(x + y))
			case token.SUB:
				result.SetFloat(float64(x - y))
			case token.MUL:
				result.SetFloat(float64(x * y))
			case token.QUO:
				result.SetFloat(float64(x / y))
			default:
				return Value{}, ErrRuntime
			}
			return Value{typ: resultType, data: result}, nil
		}
		switch operator {
		case token.ADD:
			result.SetFloat(a + b)
		case token.SUB:
			result.SetFloat(a - b)
		case token.MUL:
			result.SetFloat(a * b)
		case token.QUO:
			result.SetFloat(a / b)
		default:
			return compare(a < b, a == b)
		}
	default:
		return Value{}, ErrRuntime
	}
	return Value{typ: resultType, data: result}, nil
}

func shiftCount(value Value) (uint64, error) {
	if value.reference != 0 || !value.data.IsValid() {
		return 0, ErrCandidate
	}
	switch value.data.Kind() {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		if value.data.Int() < 0 {
			return 0, ErrCandidate
		}
		return uint64(value.data.Int()), nil
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return value.data.Uint(), nil
	}
	return 0, ErrRuntime
}
func indexNumber(value Value) (int64, error) {
	count, err := shiftCount(value)
	if err != nil {
		return 0, err
	}
	if count > uint64(^uint64(0)>>1) {
		return 0, ErrCandidate
	}
	return int64(count), nil
}
