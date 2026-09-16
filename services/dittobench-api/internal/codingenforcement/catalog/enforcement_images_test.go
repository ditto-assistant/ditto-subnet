package catalog

import (
	"os"
	"slices"
	"testing"
)

// enforcementImagesVectorFile is shared with the Python evidence tool's tests.
const enforcementImagesVectorFile = "testdata/enforcement-images-vector-v1.json"

func TestEnforcementImagesVectorAgreesWithPython(t *testing.T) {
	raw, err := os.ReadFile(enforcementImagesVectorFile)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	vector := decoded.(map[string]any)
	valid := []byte(vector["valid"].(string))
	images, err := ParseEnforcementImages(valid)
	if err != nil {
		t.Fatal(err)
	}
	if images.SHA256 != vector["valid_sha256"] {
		t.Fatalf("digest = %s", images.SHA256)
	}
	rust := images.Images["rust"]
	if !slices.Contains(rust.TestArgv["hidden"], "--authority") || slices.Equal(rust.BuildArgv, images.Images["go"].BuildArgv) {
		t.Fatalf("per-language commands were not kept: %+v", rust)
	}
	for group, argv := range vector["rust_test_argv"].(map[string]any) {
		if !slices.Equal(rust.TestArgv[group], vectorArgv(argv)) || !RustTestArgv(vectorArgv(argv), group) {
			t.Errorf("rust %s vector command = %v", group, rust.TestArgv[group])
		}
	}
	if _, err := ParseEnforcementImages([]byte(vector["noncanonical"].(string))); err == nil {
		t.Error("non-canonical document accepted")
	}
	refused := vector["refused"].(map[string]any)
	if len(refused) < 10 {
		t.Fatalf("refusal vectors = %d", len(refused))
	}
	for name, document := range refused {
		if _, err := ParseEnforcementImages([]byte(document.(string))); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

func vectorArgv(value any) []string {
	items := value.([]any)
	result := make([]string, len(items))
	for index, item := range items {
		result[index] = item.(string)
	}
	return result
}
