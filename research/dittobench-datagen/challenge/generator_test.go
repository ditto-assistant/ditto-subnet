package challenge

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestGenerateRequestValidate(t *testing.T) {
	ok := NewGenerateRequest(SeedFromInt64(1), testHotkey, 16)
	if err := ok.Validate(); err != nil {
		t.Fatal(err)
	}
	if ok.SeedInt64() == 0 || ok.SeedInt64() == NewGenerateRequest(SeedFromInt64(2), testHotkey, 16).SeedInt64() {
		t.Error("seed fold should be non-trivial")
	}
	var round GenerateRequest
	if err := json.Unmarshal(ok.Bytes(), &round); err != nil || round != ok {
		t.Fatalf("request round trip: %v %+v", err, round)
	}
	for name, mut := range map[string]func(*GenerateRequest){
		"contract": func(r *GenerateRequest) { r.Contract = "x" },
		"seed":     func(r *GenerateRequest) { r.Seed = "ABC" },
		"hotkey":   func(r *GenerateRequest) { r.MinerHotkey = "nope" },
		"cases":    func(r *GenerateRequest) { r.Cases = MaxCases + 1 },
		"bench":    func(r *GenerateRequest) { r.GradingBenchVersion = 12 },
	} {
		r := ok
		mut(&r)
		if r.Validate() == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestValidateGeneratedRejectsInvalidRequests(t *testing.T) {
	req := NewGenerateRequest(SeedFromInt64(1), testHotkey, 16)
	p := ReferencePack(req.SeedInt64(), testHotkey)
	p.Generator.Seed = req.Seed
	raw, _ := json.Marshal(p)
	req.Seed = "not-a-seed"
	if r := ValidateGenerated(raw, req); r.Valid || !hasRule(r, "", "binding.request") {
		t.Fatalf("invalid request validated: %+v", r)
	}
}

func TestValidateGeneratedBinding(t *testing.T) {
	p := ReferencePack(4, testHotkey)
	raw, _ := json.Marshal(p)
	req := NewGenerateRequest(SeedFromInt64(4), testHotkey, len(p.Cases))
	if r := ValidateGenerated(raw, req); !r.Valid {
		t.Fatalf("bound pack invalid: %+v", r.Issues)
	}
	wrongSeed := req
	wrongSeed.Seed = SeedFromInt64(5)
	wrongCount := req
	wrongCount.Cases = len(p.Cases) + 1
	wrongKey := req
	wrongKey.MinerHotkey = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
	for name, r := range map[string]GenerateRequest{"binding.seed": wrongSeed, "binding.cases": wrongCount, "binding.miner_hotkey": wrongKey} {
		rep := ValidateGenerated(raw, r)
		if rep.Valid || !hasRule(rep, "", name) {
			t.Errorf("%s: %+v", name, rep.Issues)
		}
	}
}

var minimalGenerator = []SourceFile{
	{Path: "go.mod", Data: []byte("module example.com/gen\n\ngo 1.23\n")},
	{Path: "main.go", Data: []byte("package main\n\nfunc main() {}\n")},
}

func TestArchiveRoundTripDeterministic(t *testing.T) {
	a, err := PackArchive(minimalGenerator)
	if err != nil {
		t.Fatal(err)
	}
	rev := []SourceFile{minimalGenerator[1], minimalGenerator[0]}
	b, _ := PackArchive(rev)
	if !bytes.Equal(a, b) {
		t.Fatal("PackArchive depends on input order")
	}
	info, files, err := InspectArchive(a)
	if err != nil {
		t.Fatal(err)
	}
	if info.Module != "example.com/gen" || info.GoVersion != "1.23" || info.Files != 2 || info.Vendored || len(files) != 2 {
		t.Errorf("info: %+v", info)
	}
	if info.TreeSHA256 != TreeDigest(minimalGenerator) || info.SHA256 != Digest(a) {
		t.Error("identity mismatch")
	}
	dir := t.TempDir()
	if _, err := ExtractArchive(a, dir); err != nil {
		t.Fatal(err)
	}
	collected, err := CollectSource(dir)
	if err != nil || TreeDigest(collected) != info.TreeSHA256 {
		t.Fatalf("collect: %v", err)
	}
}

func rawArchive(t *testing.T, entries []tar.Header, bodies []string) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for i := range entries {
		h := entries[i]
		h.Size = int64(len(bodies[i]))
		if h.Typeflag == 0 {
			h.Typeflag = tar.TypeReg
		}
		if h.Typeflag != tar.TypeReg {
			h.Size = 0
		}
		h.Mode = 0o644
		if err := tw.WriteHeader(&h); err != nil {
			t.Fatal(err)
		}
		if h.Size > 0 {
			tw.Write([]byte(bodies[i]))
		}
	}
	tw.Close()
	gz.Close()
	return buf.Bytes()
}

func TestInspectArchiveRejects(t *testing.T) {
	gomod := "module x\n\ngo 1.23\n"
	main := "package main\nfunc main(){}\n"
	cases := map[string]struct {
		hdrs   []tar.Header
		bodies []string
		want   string
	}{
		"escape":        {[]tar.Header{{Name: "go.mod"}, {Name: "../main.go"}}, []string{gomod, main}, "escapes"},
		"absolute":      {[]tar.Header{{Name: "go.mod"}, {Name: "/main.go"}}, []string{gomod, main}, "relative"},
		"symlink":       {[]tar.Header{{Name: "go.mod"}, {Name: "main.go", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"}}, []string{gomod, ""}, "regular file"},
		"duplicate":     {[]tar.Header{{Name: "go.mod"}, {Name: "main.go"}, {Name: "./main.go"}}, []string{gomod, main, main}, "repeats"},
		"no go.mod":     {[]tar.Header{{Name: "main.go"}}, []string{main}, "go.mod"},
		"no root go":    {[]tar.Header{{Name: "go.mod"}, {Name: "cmd/main.go"}}, []string{gomod, main}, "root"},
		"new toolchain": {[]tar.Header{{Name: "go.mod"}, {Name: "main.go"}}, []string{"module x\n\ngo 1.99\n", main}, "builder runs"},
		"unvendored":    {[]tar.Header{{Name: "go.mod"}, {Name: "main.go"}}, []string{"module x\n\ngo 1.23\n\nrequire example.com/y v1.0.0\n", main}, "vendor"},
	}
	for name, c := range cases {
		_, _, err := InspectArchive(rawArchive(t, c.hdrs, c.bodies))
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if _, _, err := InspectArchive([]byte("not gzip")); err == nil {
		t.Error("non-gzip accepted")
	}

	// A go.mod line past the scanner's default token must not end the scan
	// and hide a later require from the vendoring check.
	long := "module x\n\n// " + strings.Repeat("x", 70_000) + "\n\ngo 1.23\n\nrequire example.com/y v1.0.0\n"
	if _, _, err := InspectArchive(rawArchive(t, []tar.Header{{Name: "go.mod"}, {Name: "main.go"}}, []string{long, main})); err == nil {
		t.Error("a long go.mod line hid an unvendored requirement")
	}

	// A corrupt gzip trailer after the tar end markers is caught.
	good := rawArchive(t, []tar.Header{{Name: "go.mod"}, {Name: "main.go"}}, []string{gomod, main})
	corrupt := append([]byte(nil), good...)
	corrupt[len(corrupt)-5] ^= 0xff // inside the CRC32
	if _, _, err := InspectArchive(corrupt); err == nil {
		t.Error("corrupt gzip trailer accepted")
	}
	twoMembers := append(append([]byte(nil), good...), good...)
	if _, _, err := InspectArchive(twoMembers); err == nil {
		t.Error("a second gzip member was accepted")
	}
}

func TestInspectArchiveRejectsNonFilePayloads(t *testing.T) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	// A directory header claiming a payload: the tar reader would skip it on
	// the next Next(), decompressing it without any bound.
	hdr := tar.Header{Name: "vendor/", Typeflag: tar.TypeDir, Size: 1 << 20, Mode: 0o755}
	if err := tw.WriteHeader(&hdr); err != nil {
		t.Fatal(err)
	}
	gz.Write(make([]byte, 512))
	tw.Flush()
	gz.Close()
	if _, _, err := InspectArchive(buf.Bytes()); err == nil || !strings.Contains(err.Error(), "payload") {
		t.Fatalf("directory payload accepted: %v", err)
	}
}

func TestExtractArchiveRequiresAnEmptyDirectory(t *testing.T) {
	raw := rawArchive(t, []tar.Header{{Name: "go.mod"}, {Name: "main.go"}}, []string{"module x\n\ngo 1.23\n", "package main\nfunc main(){}\n"})
	dir := t.TempDir()
	if err := os.Symlink(t.TempDir(), filepath.Join(dir, "vendor")); err != nil {
		t.Fatal(err)
	}
	if _, err := ExtractArchive(raw, dir); err == nil {
		t.Fatal("extracting into a non-empty directory was allowed")
	}
	if _, err := ExtractArchive(raw, t.TempDir()); err != nil {
		t.Fatal(err)
	}
}

func TestArchiveRefusesAFileThatIsAlsoADirectory(t *testing.T) {
	mod, main := "module x\n\ngo 1.23\n", "package main\nfunc main(){}\n"
	for _, order := range [][2]string{{"gen.go", "gen.go/extra.go"}, {"gen.go/extra.go", "gen.go"}} {
		raw := rawArchive(t,
			[]tar.Header{{Name: "go.mod"}, {Name: order[0]}, {Name: order[1]}},
			[]string{mod, main, main})
		if _, _, err := InspectArchive(raw); err == nil || !strings.Contains(err.Error(), "is under file") {
			t.Fatalf("%v: %v", order, err)
		}
	}
}

func TestFingerprintFieldsCannotShiftIntoEachOther(t *testing.T) {
	a := Case{MemoryCase: protocol.MemoryCase{Question: "q\x00a", ExpectedAnswer: "b"}}
	b := Case{MemoryCase: protocol.MemoryCase{Question: "q", ExpectedAnswer: "a\x00b"}}
	if Fingerprint(a) == Fingerprint(b) {
		t.Fatal("distinct cases share a fingerprint")
	}
	if Fingerprint(a) != Fingerprint(a) {
		t.Fatal("fingerprint is not deterministic")
	}
}

// TestArchiveVectorGolden pins TreeDigest for Platform's Python port
// (apps/platform/ditto/tests/test_generator_archive.py).
func TestArchiveVectorGolden(t *testing.T) {
	files := append([]SourceFile(nil), minimalGenerator...)
	files = append(files, SourceFile{Path: "families/a.go", Data: []byte("package families\n")}, SourceFile{Path: "vendor/modules.txt", Data: []byte("# none\n")})
	raw, err := PackArchive(files)
	if err != nil {
		t.Fatal(err)
	}
	info, _, err := InspectArchive(raw)
	if err != nil {
		t.Fatal(err)
	}
	type vector struct {
		ArchiveBase64 []byte      `json:"archive_base64"`
		Expected      ArchiveInfo `json:"expected"`
	}
	path := filepath.Join("testdata", "archive_vector.json")
	if *update {
		b, _ := json.MarshalIndent(vector{ArchiveBase64: raw, Expected: info}, "", "  ")
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var v vector
	if err := json.Unmarshal(b, &v); err != nil {
		t.Fatal(err)
	}
	got, _, err := InspectArchive(v.ArchiveBase64)
	if err != nil || got != v.Expected || got.TreeSHA256 != info.TreeSHA256 {
		t.Fatalf("archive vector drifted: %v %+v", err, got)
	}
}
