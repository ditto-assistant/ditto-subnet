package codinggobuild

import (
	"go/parser"
	"go/token"
	"strings"
	"testing"
)

func TestBridgeContainsOnlyApprovedAPIBindings(t *testing.T) {
	body, err := BridgeSource("subject", []string{"Zulu", "alpha"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := parser.ParseFile(token.NewFileSet(), "bridge.go", body, 0); err != nil {
		t.Fatal(err)
	}
	text := string(body)
	if !strings.Contains(text, `"alpha": dittobenchReflect.ValueOf(alpha)`) || !strings.Contains(text, `"Zulu":`) || strings.Contains(text, "FUNCTION_BINDINGS") {
		t.Fatal("approved API bindings missing")
	}
	if strings.Contains(text, "passed") || strings.Contains(text, "expected_total") {
		t.Fatal("grading authority entered candidate bridge")
	}
	for _, names := range [][]string{{"x; panic(1)"}, {"init"}, {"a", "a"}, {"DittobenchCandidateBridgeV1"}} {
		if _, err := BridgeSource("subject", names); err == nil {
			t.Fatal("invalid binding accepted")
		}
	}
}
func TestMainUsesQuotedModuleIdentity(t *testing.T) {
	body, err := MainSource("example.invalid/subject")
	if err != nil || !strings.Contains(string(body), `"example.invalid/subject"`) {
		t.Fatal(err)
	}
	if _, err := MainSource("bad\npath"); err == nil {
		t.Fatal("module injection accepted")
	}
}
