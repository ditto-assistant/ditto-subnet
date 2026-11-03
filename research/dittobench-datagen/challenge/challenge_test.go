package challenge

import (
	"encoding/json"
	"strings"
	"testing"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

func TestReferencePackValidates(t *testing.T) {
	t.Parallel()
	for _, seed := range []int64{1, 2, 42, 1337, 99991} {
		r := Validate(ReferencePack(seed, testHotkey))
		if !r.Valid {
			b, _ := json.MarshalIndent(r.Issues, "", "  ")
			t.Fatalf("seed %d: reference pack invalid:\n%s", seed, b)
		}
		if r.CaseCount != 16 || len(r.Cases) != 16 {
			t.Fatalf("seed %d: case count %d refs %d", seed, r.CaseCount, len(r.Cases))
		}
		refs := ReferencePack(seed, testHotkey).Refs()
		for i := range refs {
			if r.Cases[i] != refs[i] {
				t.Fatalf("seed %d: report ref %d = %+v, want %+v", seed, i, r.Cases[i], refs[i])
			}
		}
	}
}

func digestOf(t *testing.T, p Pack) string {
	t.Helper()
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return Digest(raw)
}

func TestReferencePackDeterministic(t *testing.T) {
	a, b, c := digestOf(t, ReferencePack(7, testHotkey)), digestOf(t, ReferencePack(7, testHotkey)), digestOf(t, ReferencePack(8, testHotkey))
	if a != b || a == c {
		t.Fatalf("digest determinism: %s %s %s", a, b, c)
	}
}

func TestValidateBytesPinsTheUploadedBytes(t *testing.T) {
	raw, _ := json.MarshalIndent(ReferencePack(7, testHotkey), "", "  ")
	r := ValidateBytes(raw)
	if !r.Valid || r.PackSHA256 != Digest(raw) {
		t.Fatalf("report %+v", r)
	}
	bad := ValidateBytes([]byte(`{"contract":`))
	if bad.Valid || len(bad.Issues) != 1 || bad.Issues[0].Rule != "decode" || bad.PackSHA256 == "" {
		t.Fatalf("decode failure report %+v", bad)
	}
}

func TestDecodeRoundTripAndStrictness(t *testing.T) {
	p := ReferencePack(3, testHotkey)
	raw, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got, err := Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	if digestOf(t, got) != digestOf(t, p) {
		t.Fatalf("round trip changed content")
	}
	if _, err := Decode([]byte(strings.Replace(string(raw), `"contract"`, `"contrakt"`, 1))); err == nil {
		t.Fatal("unknown field accepted")
	}
	if _, err := Decode(append(raw, []byte(" {}")...)); err == nil {
		t.Fatal("trailing data accepted")
	}
	if _, err := Decode(make([]byte, MaxPackBytes+1)); err != ErrTooLarge {
		t.Fatalf("oversize: %v", err)
	}
}
