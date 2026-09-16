package catalog

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// Pinned identically in ditto/tests/test_coding_native_enforcement_evidence.py.
const (
	goldenRecordSHA256    = "8ed4ce9e831a20ae9bee4c60a865c44a3340c6098d4074615938c7446d8aff6c"
	canonicalVectorSHA256 = "1948b8f75bd3f0c25825ed268d2390e89ffe1993a37790ee740c19e5cd491a74"
)

func digestHex(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// trickyVector mirrors TRICKY in the Python test.
func trickyVector() map[string]any {
	return map[string]any{
		"ascii":      "plain <>&'/",
		"controls":   "\x00\x01\b\t\n\x0b\f\r\x1f\x7f",
		"escapes":    "quote\" backslash\\ slash/",
		"separators": "line\u2028paragraph\u2029",
		"unicode":    "caf\u00e9 \u65e5\u672c \U0001f600",
		"integers":   []any{0, -1, int64(9223372036854775807), int64(-9223372036854775808)},
		"literals":   []any{true, false, nil},
		"empty":      map[string]any{"list": []any{}, "object": map[string]any{}, "string": ""},
		"order":      map[string]any{"b": 1, "a": 2, "B": 3, "_": 4, "aa": 5, "\u00e9": 6},
	}
}

func TestCanonicalVectorMatchesPythonBytes(t *testing.T) {
	raw, err := os.ReadFile("testdata/canonical-vector-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := Canonical(trickyVector())
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(encoded, raw) {
		t.Fatalf("canonical bytes differ from the Python vector:\n%s\n%s", encoded, raw)
	}
	if got := digestHex(raw); got != canonicalVectorSHA256 {
		t.Fatalf("vector digest = %s", got)
	}
	decoded, err := ParseCanonical(raw)
	if err != nil {
		t.Fatal(err)
	}
	again, err := Canonical(decoded)
	if err != nil || !bytes.Equal(again, raw) {
		t.Fatalf("decode and re-encode changed the vector: %v", err)
	}
}

func TestGoldenRecordMatchesPythonBytes(t *testing.T) {
	raw, err := os.ReadFile("testdata/golden-record-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	if got := digestHex(raw); got != goldenRecordSHA256 {
		t.Fatalf("golden record digest = %s", got)
	}
	decoded, err := ParseCanonical(raw)
	if err != nil {
		t.Fatal(err)
	}
	record := decoded.(map[string]any)
	if record["schema"] != RecordSchema || record["kind"] != "cleanup_recovery" || record["coverage"] != Coverage {
		t.Fatalf("golden record identity = %v %v %v", record["schema"], record["kind"], record["coverage"])
	}
	loaded, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	required, err := loaded.RequiredInstances("cleanup_recovery", Endpoints{})
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, phase := range record["phases"].([]any) {
		for _, probe := range phase.(map[string]any)["probes"].([]any) {
			fields := probe.(map[string]any)
			got = append(got, fields["id"].(string))
			observed := fields["observed"]
			var expect Expectation
			encoded, _ := Canonical(fields["expect"])
			if err := json.Unmarshal(encoded, &expect); err != nil {
				t.Fatal(err)
			}
			matched, err := Evaluate(expect, observed, SubordinateIDs{UIDStart: 100000, UIDCount: 65536, GIDStart: 100000, GIDCount: 65536}, loaded.OutcomeSet(), loaded.Tolerances)
			if err != nil || matched != fields["matched"].(bool) {
				t.Fatalf("%s: matched = %v, %v", fields["id"], matched, err)
			}
		}
	}
	var want []string
	for _, instance := range required {
		want = append(want, instance.ID)
	}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("golden probes = %v, catalog requires %v", got, want)
	}
}

func TestCanonicalRefusals(t *testing.T) {
	for name, raw := range map[string]string{
		"pretty":           `{"a": 1}`,
		"newline":          "{\"a\":1}\n",
		"unsorted":         `{"b":1,"a":2}`,
		"raw separator":    "{\"a\":\"\u2028\"}",
		"uppercase escape": `{"a":"\u00E9"}`,
		"negative zero":    `{"a":-0}`,
		"duplicate":        `{"a":1,"a":1}`,
		"float":            `{"a":1.0}`,
		"exponent":         `{"a":1e3}`,
		"too large":        `{"a":9223372036854775808}`,
		"lone surrogate":   `{"a":"\udc00"}`,
		"invalid utf8":     "{\"a\":\"\xff\"}",
		"trailing":         `{"a":1}{}`,
		"deep":             strings.Repeat("[", 20) + strings.Repeat("]", 20),
	} {
		if _, err := ParseCanonical([]byte(raw)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := Canonical(map[string]any{"a": 1.5}); err == nil {
		t.Error("float encoded")
	}
	if _, err := Canonical(map[string]any{"a": "\xff"}); err == nil {
		t.Error("invalid UTF-8 encoded")
	}
}

func TestDecodeRefusesBeforeReencoding(t *testing.T) {
	for name, raw := range map[string]string{
		"duplicate key":    `{"a":1,"a":2}`,
		"nested duplicate": `{"a":{"b":1,"b":1}}`,
		"float":            `{"a":1.5}`,
		"integral float":   `{"a":1.0}`,
		"exponent":         `[1e3]`,
		"too large":        `[9223372036854775808]`,
		"trailing":         `{} []`,
		"deep":             strings.Repeat("[", 20) + strings.Repeat("]", 20),
		"invalid utf8":     "[\"\xff\"]",
	} {
		if _, err := Decode([]byte(raw)); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
	duplicate := bytes.Replace(Bytes(), []byte(`"coverage": "same_boot",`), []byte(`"coverage": "same_boot", "coverage": "same_boot",`), 1)
	if bytes.Equal(duplicate, Bytes()) {
		t.Fatal("catalog fixture did not change")
	}
	if _, err := Parse(duplicate); err == nil {
		t.Error("catalog with a duplicate key parsed")
	}
}
