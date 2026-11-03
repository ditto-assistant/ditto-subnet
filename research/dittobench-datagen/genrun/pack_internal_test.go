package genrun

import "testing"

func TestHasRequireAcceptsAnyWhitespace(t *testing.T) {
	for _, mod := range []string{"require x v1\n", "require\tx v1\n", "require (\n\tx v1\n)\n", "require(\nx v1\n)\n", "\trequire\t(\n"} {
		if !hasRequire([]byte("module m\n\ngo 1.23\n" + mod)) {
			t.Errorf("missed require in %q", mod)
		}
	}
	if hasRequire([]byte("module m\n\ngo 1.23\n// require x\nrequired x\n")) {
		t.Error("matched a non-directive")
	}
}
