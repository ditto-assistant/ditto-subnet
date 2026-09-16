package subject

import "testing"

func TestAdd(t *testing.T) {
	if Add(2, 3) != 5 {
		t.Fatal("synthetic assertion")
	}
}

func TestNil(t *testing.T) {
	if Length(nil) != 0 {
		t.Fatal("synthetic nil assertion")
	}
}
