// Package codinggooracle owns private Go test syntax and types, never candidate
// execution. Test source must not be linked into the untrusted candidate binary.
package codinggooracle

import (
	"errors"
	"go/ast"
	"go/build/constraint"
	"go/parser"
	"go/token"
	"go/types"
	"path"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

var (
	ErrSuite          = errors.New("unsupported private Go suite")
	ErrCandidateTypes = errors.New("candidate Go package is not type compatible")
)

// Source is already authenticated Go source. CandidateSources must be selected
// non-test package files; SupportSources are protected oracle dependencies. The
// caller owns snapshot verification, build-tag selection and immutable imports.
type Source struct {
	Name string
	Body []byte
}

// Config binds APIs independently of private test imports. Importer must resolve
// only preverified offline type information; this package never shells out or
// provides an importer.Default/network fallback. Expected counts are authoritative.
type Config struct {
	PackagePath      string
	CandidateSources []Source
	SupportSources   []Source // Protected helper/test files; their tests are not selected.
	Functions        []string
	Methods          []string // Receiver.Method, including unexported APIs only for internal tests.
	ExpectedTests    int
	Importer         types.Importer
}

// Suite contains private AST/type information. Do not log, export or deliver it
// to a candidate. Only API arguments may later cross the execution boundary.
type Suite struct {
	file    *ast.File
	tests   []*ast.FuncDecl
	info    *types.Info
	api     map[types.Object]bool
	methods map[*types.Func]bool
	helpers map[*types.Func]*ast.FuncDecl
}

func (s *Suite) TestCount() int { return len(s.tests) }

type imports struct {
	api      *types.Package
	delegate types.Importer
}

func (i imports) Import(name string) (*types.Package, error) {
	if name == i.api.Path() {
		return i.api, nil
	}
	return i.delegate.Import(name)
}

func isTest(name string) bool {
	if !strings.HasPrefix(name, "Test") {
		return false
	}
	if name == "Test" {
		return true
	}
	r, _ := utf8.DecodeRuneInString(name[4:])
	return !unicode.IsLower(r)
}

func boundedTree(file *ast.File, remaining *int) bool {
	depth, valid := 0, true
	ast.Inspect(file, func(node ast.Node) bool {
		if node == nil {
			depth--
			return true
		}
		depth++
		*remaining--
		if depth > 128 || *remaining < 0 {
			depth--
			valid = false
			return false
		}
		return true
	})
	return valid
}

// Compile performs no candidate execution and never compiles the private suite.
// Both internal package tests and external package_test tests are supported.
func Compile(source []byte, config Config) (*Suite, error) {
	if len(source) == 0 || len(source) > 512<<10 || config.Importer == nil ||
		config.PackagePath == "" || config.ExpectedTests < 1 || config.ExpectedTests > 1024 ||
		len(config.CandidateSources) < 1 || len(config.CandidateSources) > 128 ||
		len(config.Functions)+len(config.Methods) == 0 {
		return nil, ErrSuite
	}
	switch config.PackagePath {
	case "testing", "reflect", "bytes", "unsafe", "time":
		return nil, ErrSuite // Candidate code cannot occupy an oracle builtin namespace.
	}
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "private_test.go", source, parser.ParseComments|parser.SkipObjectResolution)
	if err != nil {
		return nil, ErrSuite
	}
	remaining := 200000
	if !boundedTree(file, &remaining) {
		return nil, ErrSuite
	}
	privateFiles := []*ast.File{file}
	privateBytes := len(source)
	if len(config.SupportSources) > 16 {
		return nil, ErrSuite
	}
	for index, support := range config.SupportSources {
		privateBytes += len(support.Body)
		if privateBytes > 1<<20 {
			return nil, ErrSuite
		}
		parsed, err := parser.ParseFile(fset, "support_"+strconv.Itoa(index)+"_test.go", support.Body, parser.ParseComments|parser.SkipObjectResolution)
		if err != nil || parsed.Name.Name != file.Name.Name || !boundedTree(parsed, &remaining) {
			return nil, ErrSuite
		}
		privateFiles = append(privateFiles, parsed)
	}
	var tests []*ast.FuncDecl
	var helpers []*ast.FuncDecl
	for fileIndex, private := range privateFiles {
		for _, group := range private.Comments {
			for _, comment := range group.List {
				if strings.HasPrefix(comment.Text, "//go:") || constraint.IsPlusBuild(comment.Text) {
					return nil, ErrSuite
				}
			}
		}
		for _, declaration := range private.Decls {
			switch node := declaration.(type) {
			case *ast.GenDecl:
				if node.Tok != token.IMPORT {
					return nil, ErrSuite
				}
				for _, raw := range node.Specs {
					item, ok := raw.(*ast.ImportSpec)
					if !ok || (item.Name != nil && (item.Name.Name == "." || item.Name.Name == "_")) {
						return nil, ErrSuite
					}
					name, err := strconv.Unquote(item.Path.Value)
					if err != nil || (name != "testing" && name != "reflect" && name != "bytes" && name != "time" && name != config.PackagePath) {
						return nil, ErrSuite
					}
				}
			case *ast.FuncDecl:
				if node.Recv != nil || node.Body == nil || node.Name.Name == "init" ||
					(node.Type.TypeParams != nil && len(node.Type.TypeParams.List) != 0) {
					return nil, ErrSuite
				}
				if !isTest(node.Name.Name) {
					if len(helpers) >= 32 {
						return nil, ErrSuite
					}
					helpers = append(helpers, node)
					continue
				}
				if node.Type.Params == nil || len(node.Type.Params.List) != 1 || len(node.Type.Params.List[0].Names) != 1 ||
					node.Type.Params.List[0].Names[0].Name == "_" || (node.Type.Results != nil && len(node.Type.Results.List) != 0) {
					return nil, ErrSuite
				}
				if fileIndex == 0 {
					tests = append(tests, node)
				}
			default:
				return nil, ErrSuite
			}
		}
	}
	if len(tests) != config.ExpectedTests {
		return nil, ErrSuite
	}
	var files []*ast.File
	seen, total := map[string]bool{}, 0
	for _, item := range config.CandidateSources {
		if item.Name == "" || path.IsAbs(item.Name) || path.Clean(item.Name) != item.Name ||
			strings.HasPrefix(item.Name, "../") || !strings.HasSuffix(item.Name, ".go") ||
			strings.HasSuffix(item.Name, "_test.go") || seen[item.Name] {
			return nil, ErrSuite
		}
		seen[item.Name] = true
		total += len(item.Body)
		if total > 8<<20 {
			return nil, ErrSuite
		}
		parsed, err := parser.ParseFile(fset, "candidate/"+item.Name, item.Body, parser.SkipObjectResolution)
		if err != nil || !boundedTree(parsed, &remaining) {
			return nil, ErrCandidateTypes
		}
		files = append(files, parsed)
	}
	info := &types.Info{Types: map[ast.Expr]types.TypeAndValue{}, Defs: map[*ast.Ident]types.Object{}, Uses: map[*ast.Ident]types.Object{}, Selections: map[*ast.SelectorExpr]*types.Selection{}}
	typeConfig := &types.Config{Importer: config.Importer, Sizes: types.SizesFor("gc", "amd64"), GoVersion: "go1.26"}
	apiPackage := types.NewPackage(config.PackagePath, files[0].Name.Name)
	checker := types.NewChecker(typeConfig, fset, apiPackage, info)
	if checker.Files(files) != nil {
		return nil, ErrCandidateTypes
	}
	suite := &Suite{file: file, tests: tests, info: info, api: map[types.Object]bool{}, methods: map[*types.Func]bool{}, helpers: map[*types.Func]*ast.FuncDecl{}}
	for _, name := range config.Functions {
		if !token.IsIdentifier(name) || name == "_" {
			return nil, ErrSuite
		}
		function, ok := apiPackage.Scope().Lookup(name).(*types.Func)
		if !ok {
			return nil, ErrCandidateTypes
		}
		if suite.api[function] {
			return nil, ErrSuite
		}
		suite.api[function] = true
	}
	for _, name := range config.Methods {
		parts := strings.Split(name, ".")
		if len(parts) != 2 || !token.IsIdentifier(parts[0]) || !token.IsIdentifier(parts[1]) {
			return nil, ErrSuite
		}
		object, ok := apiPackage.Scope().Lookup(parts[0]).(*types.TypeName)
		if !ok {
			return nil, ErrCandidateTypes
		}
		method, _, _ := types.LookupFieldOrMethod(types.NewPointer(object.Type()), true, apiPackage, parts[1])
		function, ok := method.(*types.Func)
		if !ok {
			return nil, ErrCandidateTypes
		}
		if suite.methods[function] {
			return nil, ErrSuite
		}
		suite.methods[function] = true
	}
	switch file.Name.Name {
	case apiPackage.Name():
		// Incremental checking keeps original package identity and unexported API
		// visibility without copying hidden tests into a linked candidate program.
		if checker.Files(privateFiles) != nil {
			return nil, ErrCandidateTypes
		}
	case apiPackage.Name() + "_test":
		typeConfig.Importer = imports{apiPackage, config.Importer}
		if _, err := typeConfig.Check(config.PackagePath+"_test", fset, privateFiles, info); err != nil {
			return nil, ErrCandidateTypes
		}
	default:
		return nil, ErrSuite
	}
	for _, helper := range helpers {
		function, ok := info.Defs[helper.Name].(*types.Func)
		if !ok || suite.api[function] {
			return nil, ErrSuite
		}
		suite.helpers[function] = helper
	}
	graph := map[*types.Func]map[*types.Func]bool{}
	for function, helper := range suite.helpers {
		validator := validator{suite: suite, helper: true, locals: privateLocals(helper, info), calls: map[*types.Func]bool{}}
		if !validator.block(helper.Body) {
			return nil, ErrSuite
		}
		graph[function] = validator.calls
	}
	if recursiveHelpers(graph) {
		return nil, ErrSuite
	}
	for _, test := range tests {
		parameter := info.Defs[test.Type.Params.List[0].Names[0]]
		pointer, ok := types.Unalias(parameter.Type()).(*types.Pointer)
		if !ok {
			return nil, ErrSuite
		}
		named, ok := types.Unalias(pointer.Elem()).(*types.Named)
		if !ok || named.Obj().Pkg() == nil || named.Obj().Pkg().Path() != "testing" || named.Obj().Name() != "T" {
			return nil, ErrSuite
		}
		validator := validator{suite: suite, parameter: parameter, locals: privateLocals(test, info), calls: map[*types.Func]bool{}}
		if !validator.block(test.Body) || validator.assertions == 0 {
			return nil, ErrSuite
		}
	}
	return suite, nil
}

type validator struct {
	suite      *Suite
	parameter  types.Object
	assertions int
	locals     map[types.Object]bool
	helper     bool
	calls      map[*types.Func]bool
}

func privateLocals(function *ast.FuncDecl, info *types.Info) map[types.Object]bool {
	locals := map[types.Object]bool{}
	ast.Inspect(function, func(node ast.Node) bool {
		if id, ok := node.(*ast.Ident); ok {
			object := info.Defs[id]
			switch object.(type) {
			case *types.Var, *types.Const:
				locals[object] = true
			}
		}
		return true
	})
	return locals
}

func recursiveHelpers(graph map[*types.Func]map[*types.Func]bool) bool {
	state := map[*types.Func]int{}
	var visit func(*types.Func) bool
	visit = func(function *types.Func) bool {
		if state[function] == 1 {
			return true
		}
		if state[function] == 2 {
			return false
		}
		state[function] = 1
		for child := range graph[function] {
			if visit(child) {
				return true
			}
		}
		state[function] = 2
		return false
	}
	for function := range graph {
		if visit(function) {
			return true
		}
	}
	return false
}

func (v *validator) expression(expression ast.Expr) bool {
	if expression == nil {
		return false
	}
	info := v.suite.info
	if info.Types[expression].Value != nil {
		valid := true
		ast.Inspect(expression, func(node ast.Node) bool {
			if identifier, ok := node.(*ast.Ident); ok {
				if object, ok := info.Uses[identifier].(*types.Const); ok &&
					object.Parent() != types.Universe && !v.locals[object] &&
					(object.Pkg() == nil || object.Pkg().Path() != "time") {
					valid = false
				}
			}
			return true
		})
		return valid
	} // Go-checked constants, never evaluation.
	switch node := expression.(type) {
	case *ast.Ident:
		object := info.Uses[node]
		if object == nil || object == v.parameter {
			return false
		}
		switch object.(type) {
		case *types.Var, *types.Const:
			return v.locals[object]
		case *types.Nil:
			return true
		}
	case *ast.ParenExpr:
		return v.expression(node.X)
	case *ast.UnaryExpr:
		return (node.Op == token.NOT || node.Op == token.ADD || node.Op == token.SUB || node.Op == token.XOR) && v.expression(node.X)
	case *ast.BinaryExpr:
		return v.expression(node.X) && v.expression(node.Y)
	case *ast.IndexExpr:
		return v.expression(node.X) && v.expression(node.Index)
	case *ast.SliceExpr:
		return v.expression(node.X) && (node.Low == nil || v.expression(node.Low)) && (node.High == nil || v.expression(node.High)) && (node.Max == nil || v.expression(node.Max))
	case *ast.CompositeLit:
		for _, value := range node.Elts {
			if pair, ok := value.(*ast.KeyValueExpr); ok {
				if !v.expression(pair.Value) {
					return false
				}
				if _, structure := info.TypeOf(node).Underlying().(*types.Struct); !structure && !v.expression(pair.Key) {
					return false
				}
			} else if !v.expression(value) {
				return false
			}
		}
		return true
	case *ast.SelectorExpr:
		selection := info.Selections[node]
		if selection == nil {
			object := info.Uses[node.Sel]
			return object != nil && object.Pkg() != nil && object.Pkg().Path() == "time" && object.Name() == "UTC"
		}
		return selection != nil && selection.Kind() == types.FieldVal && v.expression(node.X)
	case *ast.CallExpr:
		if node.Ellipsis.IsValid() {
			return false
		}
		for _, argument := range node.Args {
			if !v.expression(argument) {
				return false
			}
		}
		if info.Types[node.Fun].IsType() {
			_, basic := info.TypeOf(node.Fun).Underlying().(*types.Basic)
			return basic && len(node.Args) == 1
		}
		var object types.Object
		switch target := node.Fun.(type) {
		case *ast.Ident:
			object = info.Uses[target]
		case *ast.SelectorExpr:
			if selection := info.Selections[target]; selection != nil {
				method, ok := selection.Obj().(*types.Func)
				errorMethod := ok && method.Name() == "Error" && types.Identical(selection.Recv(), types.Universe.Lookup("error").Type())
				return ok && selection.Kind() == types.MethodVal && (v.suite.methods[method] || errorMethod || trustedTimeMethod(selection)) && v.expression(target.X)
			}
			object = info.Uses[target.Sel]
		default:
			return false
		}
		if v.suite.api[object] {
			return true
		}
		if function, ok := object.(*types.Func); ok && v.suite.helpers[function] != nil {
			v.calls[function] = true
			return true
		}
		if builtin, ok := object.(*types.Builtin); ok {
			return builtin.Name() == "len" || builtin.Name() == "cap"
		}
		if function, ok := object.(*types.Func); ok && function.Pkg() != nil {
			return (function.Pkg().Path() == "reflect" && function.Name() == "DeepEqual") ||
				(function.Pkg().Path() == "bytes" && function.Name() == "Equal") ||
				(function.Pkg().Path() == "time" && (function.Name() == "Date" || function.Name() == "FixedZone"))
		}
	}
	return false
}

func trustedTimeMethod(selection *types.Selection) bool {
	receiver := types.Unalias(selection.Recv())
	if pointer, ok := receiver.(*types.Pointer); ok {
		receiver = types.Unalias(pointer.Elem())
	}
	named, ok := receiver.(*types.Named)
	if !ok || named.Obj().Pkg() == nil || named.Obj().Pkg().Path() != "time" || named.Obj().Name() != "Time" {
		return false
	}
	return selection.Obj().Name() == "Add" || selection.Obj().Name() == "In"
}

func (v *validator) statement(statement ast.Stmt) bool {
	switch node := statement.(type) {
	case *ast.AssignStmt:
		if node.Tok != token.DEFINE && node.Tok != token.ASSIGN {
			return false
		}
		for _, target := range node.Lhs {
			identifier, ok := target.(*ast.Ident)
			if !ok || (identifier.Name != "_" && !v.locals[v.suite.info.ObjectOf(identifier)]) {
				return false
			}
		}
		for _, value := range node.Rhs {
			if !v.expression(value) {
				return false
			}
		}
		return true
	case *ast.DeclStmt:
		declaration, ok := node.Decl.(*ast.GenDecl)
		if !ok || (declaration.Tok != token.VAR && declaration.Tok != token.CONST) {
			return false
		}
		for _, raw := range declaration.Specs {
			value, ok := raw.(*ast.ValueSpec)
			if !ok {
				return false
			}
			for _, expression := range value.Values {
				if !v.expression(expression) {
					return false
				}
			}
		}
		return true
	case *ast.ExprStmt:
		_, call := node.X.(*ast.CallExpr)
		return call && v.expression(node.X)
	case *ast.BlockStmt:
		return v.block(node)
	case *ast.ReturnStmt:
		if !v.helper {
			return false
		}
		for _, value := range node.Results {
			if !v.expression(value) {
				return false
			}
		}
		return true
	case *ast.RangeStmt:
		if !v.helper || !v.expression(node.X) || (node.Tok != token.DEFINE && node.Tok != token.ILLEGAL) {
			return false
		}
		switch typ := v.suite.info.TypeOf(node.X).Underlying().(type) {
		case *types.Array, *types.Slice, *types.Map:
		case *types.Basic:
			if typ.Info()&types.IsString == 0 {
				return false
			}
		default:
			return false
		}
		return v.block(node.Body)
	case *ast.IfStmt:
		if v.helper {
			return (node.Init == nil || v.statement(node.Init)) && v.expression(node.Cond) && v.block(node.Body) && (node.Else == nil || v.statement(node.Else))
		}
		if node.Else != nil || (node.Init != nil && !v.statement(node.Init)) || !v.expression(node.Cond) || len(node.Body.List) != 1 {
			return false
		}
		statement, ok := node.Body.List[0].(*ast.ExprStmt)
		if !ok {
			return false
		}
		call, ok := statement.X.(*ast.CallExpr)
		if !ok || call.Ellipsis.IsValid() {
			return false
		}
		selector, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return false
		}
		receiver, ok := selector.X.(*ast.Ident)
		if !ok || v.suite.info.ObjectOf(receiver) != v.parameter {
			return false
		}
		switch selector.Sel.Name {
		case "Fatal", "Fatalf", "Error", "Errorf", "Fail", "FailNow":
		default:
			return false
		}
		// Diagnostics are not formatted or sent anywhere. Only side-effect-free
		// literal/local-value arguments are admitted, never calls or closures.
		for _, argument := range call.Args {
			if v.suite.info.Types[argument].Value != nil {
				continue
			}
			if _, ok := argument.(*ast.Ident); !ok || !v.expression(argument) {
				return false
			}
		}
		v.assertions++
		return true
	}
	return false
}

func (v *validator) block(block *ast.BlockStmt) bool {
	for _, statement := range block.List {
		if !v.statement(statement) {
			return false
		}
	}
	return true
}
