package codinggooracle

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/constant"
	"go/importer"
	"go/types"
	"strings"
	"testing"
)

func configuration() Config {
	return Config{
		PackagePath: "example.invalid/subject", ExpectedTests: 1, Importer: importer.Default(),
		Functions: []string{"Add", "wide"}, Methods: []string{"Counter.Add"},
		CandidateSources: []Source{{Name: "subject.go", Body: []byte(`package subject
func init(){ panic("must never execute during oracle type checking") }
func Add(a,b int) int { return a+b }
func wide() int64 { return 9223372036854775807 }
type Counter struct { Value int }
func (c *Counter) Add(n int) int { c.Value += n; return c.Value }
`)}},
	}
}

func TestExternalAndInternalSuites(t *testing.T) {
	for _, source := range []string{
		`package subject_test; import("testing"; api "example.invalid/subject"); func TestAdd(t *testing.T){ got:=api.Add(2,3); if got!=5 {t.Fatalf("wrong: %v",got)} }`,
		`package subject; import "testing"; func TestAdd(t *testing.T){ if got:=Add(2,3); got!=5 {t.Fatal("wrong")} }`,
		`package subject; import "testing"; func TestCounter(t *testing.T){ c:=Counter{Value:3}; got:=c.Add(2); if got!=5 {t.Error("wrong")} }`,
	} {
		suite, err := Compile([]byte(source), configuration())
		if err != nil {
			t.Fatalf("compile: %v", err)
		}
		if suite.TestCount() != 1 {
			t.Fatal("wrong discovered count")
		}
	}
}

func TestGoTypesRetainIntegerPrecision(t *testing.T) {
	suite, err := Compile([]byte(`package subject; import "testing"; func TestWide(t *testing.T){ if wide()!=9223372036854775807 {t.Fatal("wrong")} }`), configuration())
	if err != nil {
		t.Fatal(err)
	}
	found := false
	ast.Inspect(suite.file, func(node ast.Node) bool {
		literal, ok := node.(*ast.BasicLit)
		if !ok || literal.Value != "9223372036854775807" {
			return true
		}
		value := suite.info.Types[literal]
		if value.Type != types.Typ[types.Int64] {
			t.Fatalf("constant lost contextual Go type: %v", value.Type)
		}
		integer, exact := constant.Int64Val(value.Value)
		if !exact || integer != 9223372036854775807 {
			t.Fatal("integer was rounded")
		}
		found = true
		return true
	})
	if !found {
		t.Fatal("private literal not checked")
	}
}

func TestUnsupportedSuitesFailClosed(t *testing.T) {
	for _, body := range []string{
		``, `t.Skip("skip")`, `if false {t.Fatal("x")} else {t.Fatal("y")}`,
		`go Add(1,2)`, `defer Add(1,2)`, `f:=func(){}; f()`,
		`for i:=0;i<1;i++ {if Add(1,2)!=3 {t.Fatal("x")}}`,
		`if Add(1,2)!=3 {t.Fatalf("x",Add(3,4))}`,
		`t.Run("sub",func(t *testing.T){if Add(1,2)!=3 {t.Fatal("x")}})`,
		`if Add(1,2)!=3 {t.Log("not an assertion")}`,
		`if Add(1,2)!=3 {t.Fatal("x"); Add(3,4)}`,
		`_ = t; if Add(1,2)!=3 {t.Fatal("x")}`,
	} {
		source := `package subject; import "testing"; func TestBad(t *testing.T){` + body + `}`
		if _, err := Compile([]byte(source), configuration()); err == nil {
			t.Fatalf("accepted unsupported body %q", body)
		}
	}
}

func TestPrivateHelpersNeverBecomeCandidateAPIs(t *testing.T) {
	withHelper := `package subject; import "testing"; func oracle()int{return 5};func TestBad(t *testing.T){if Add(2,3)!=oracle(){t.Fatal("x")}}`
	suite, err := Compile([]byte(withHelper), configuration())
	if err != nil || len(suite.helpers) != 1 {
		t.Fatal("trusted private helper missing", err)
	}
	for function := range suite.helpers {
		if suite.api[function] {
			t.Fatal("private helper delegated to candidate")
		}
	}
	config := configuration()
	config.Functions = append(config.Functions, "oracle")
	if _, err := Compile([]byte(withHelper), config); err != ErrCandidateTypes {
		t.Fatal("private helper admitted as candidate API")
	}
	for _, source := range []string{
		`package subject; import("testing"; "os");func TestBad(t *testing.T){os.Exit(0);if Add(2,3)!=5{t.Fatal("x")}}`,
		`package subject; import("testing"; _ "unsafe");func TestBad(t *testing.T){if Add(2,3)!=5{t.Fatal("x")}}`,
	} {
		if _, err := Compile([]byte(source), configuration()); err == nil {
			t.Fatal("unsafe private helper accepted")
		}
	}
}

func TestCountsSignaturesAndGoTypeErrors(t *testing.T) {
	valid := `package subject;import "testing";func TestAdd(t *testing.T){if Add(2,3)!=5{t.Fatal("x")}}`
	config := configuration()
	config.ExpectedTests = 2
	if _, err := Compile([]byte(valid), config); err != ErrSuite {
		t.Fatal("count mismatch accepted")
	}
	config = configuration()
	config.Functions = []string{"wide"}
	if _, err := Compile([]byte(valid), config); err != ErrSuite {
		t.Fatal("unapproved API accepted")
	}
	for _, source := range []string{
		strings.Replace(valid, `Add(2,3)`, `Add("2",3)`, 1),
		strings.Replace(valid, `!=5`, `!=int32(5)`, 1),
	} {
		if _, err := Compile([]byte(source), configuration()); err != ErrCandidateTypes {
			t.Fatal("Go type mismatch not rejected", err)
		}
	}
	if _, err := Compile([]byte("//go:build ignore\n"+valid), configuration()); err != ErrSuite {
		t.Fatal("build-tagged suite accepted")
	}
}

func TestCandidateGlobalsAndConstantsAreNotOracleInputs(t *testing.T) {
	config := configuration()
	config.CandidateSources[0].Body = append(config.CandidateSources[0].Body,
		[]byte("\nconst SecretWant = 5\nvar shared = 5\n")...)
	for _, body := range []string{
		`if Add(2,3)!=SecretWant {t.Fatal("x")}`,
		`const want=SecretWant+1; if Add(2,3)!=want {t.Fatal("x")}`,
		`if Add(2,3)!=shared {t.Fatal("x")}`,
		`shared=5; if Add(2,3)!=5 {t.Fatal("x")}`,
	} {
		source := `package subject;import "testing";func TestBad(t *testing.T){` + body + `}`
		if _, err := Compile([]byte(source), config); err != ErrSuite {
			t.Fatalf("candidate global entered private oracle: %v", err)
		}
	}
	valid := `package subject;import "testing";func TestGood(t *testing.T){const want=5; got:=Add(2,3); got=got+0; if got!=want {t.Fatal("x")}}`
	if _, err := Compile([]byte(valid), config); err != nil {
		t.Fatal(err)
	}
}

func TestCandidateCannotImpersonateOracleBuiltinPackages(t *testing.T) {
	for _, name := range []string{"testing", "reflect", "bytes", "unsafe", "time"} {
		config := configuration()
		config.PackagePath = name
		if _, err := Compile([]byte(`package subject;import "testing";func TestX(t *testing.T){if Add(2,3)!=5 {t.Fatal("x")}}`), config); err != ErrSuite {
			t.Fatal("builtin namespace accepted", name)
		}
	}
}

func TestPrivateHelperDependenciesAndRangeRemainInParent(t *testing.T) {
	config := configuration()
	config.SupportSources = []Source{{Name: "visible_test.go", Body: []byte(`package subject;import "testing"
func contains(values []int, wanted int)bool {for _,value:=range values {if value==wanted{return true}};return false}
func TestVisible(t *testing.T){if Add(1,2)!=3{t.Fatal("wrong")}}
`)}}
	suite, err := Compile([]byte(`package subject;import "testing";func TestHidden(t *testing.T){if !contains([]int{Add(2,3)},5){t.Fatal("wrong")}}`), config)
	if err != nil || suite.TestCount() != 1 || len(suite.helpers) != 1 {
		t.Fatal("protected support helper not admitted", err)
	}
	config.SupportSources[0].Body = []byte(`package subject;func contains(values []int,wanted int)bool{return contains(values,wanted)}`)
	if _, err := Compile([]byte(`package subject;import "testing";func TestHidden(t *testing.T){if !contains([]int{Add(2,3)},5){t.Fatal("wrong")}}`), config); err != ErrSuite {
		t.Fatal("recursive helper accepted", err)
	}
}

func TestTimeConstructorsAndErrorInspectionAreExplicitBuiltins(t *testing.T) {
	config := configuration()
	config.Functions = append(config.Functions, "Window", "Failure")
	config.CandidateSources[0].Body = append(config.CandidateSources[0].Body, []byte("\nfunc Failure()error{return nil}\n")...)
	config.CandidateSources = append(config.CandidateSources, Source{Name: "window.go", Body: []byte(`package subject;import "time";func Window(a,b time.Time)bool{return a.Before(b)}`)})
	source := `package subject;import("testing";"time");func TestWindow(t *testing.T){
 a:=time.Date(2001,time.January,1,0,0,0,0,time.UTC)
 b:=time.Date(2001,time.January,2,0,0,0,0,time.FixedZone("synthetic",3600))
 if !Window(a.Add(time.Second),b.In(time.UTC)){t.Fatal("wrong")}
 err:=Failure();if err!=nil && err.Error()!="synthetic"{t.Fatal("wrong")}
}`
	if _, err := Compile([]byte(source), config); err != nil {
		t.Fatal(err)
	}
	if _, err := Compile([]byte(strings.Replace(source, "a:=time.Date(2001,time.January,1,0,0,0,0,time.UTC)", "a:=time.Now()", 1)), config); err != ErrSuite {
		t.Fatal("ambient clock accepted", err)
	}
}

func TestCompilerErrorsDoNotExposePrivateDiagnostics(t *testing.T) {
	marker := "PRIVATE-ORACLE-MARKER-DO-NOT-EXPOSE"
	_, err := Compile([]byte(`package subject;import "testing";func TestX(t *testing.T){if Add("`+marker+`",3)!=5 {t.Fatal("x")}}`), configuration())
	if err != ErrCandidateTypes || strings.Contains(err.Error(), marker) {
		t.Fatal("private diagnostic exposed", err)
	}
}

func TestPrivateObjectsCannotBecomeDebugOrWirePayloads(t *testing.T) {
	config := configuration()
	source := []byte(`package subject;import "testing";func TestPrivateMarker(t *testing.T){if Add(1,2)!=3{t.Fatal("PRIVATE-ORACLE-MARKER")}}`)
	suite, err := Compile(source, config)
	if err != nil {
		t.Fatal(err)
	}
	for _, object := range []any{Source{Name: "private.go", Body: source}, config, suite} {
		if body, err := json.Marshal(object); err == nil || len(body) != 0 {
			t.Fatal("private object serialized")
		}
		formatted := fmt.Sprintf("%v %+v %#v", object, object, object)
		if strings.Contains(formatted, "PRIVATE-ORACLE-MARKER") || strings.Contains(formatted, "TestPrivateMarker") || strings.Contains(formatted, "example.invalid") {
			t.Fatal("private debug output exposed")
		}
	}
}
