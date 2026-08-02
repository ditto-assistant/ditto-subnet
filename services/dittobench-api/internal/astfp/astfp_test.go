package astfp

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// writeCrate materializes {relpath: source} under a fresh temp dir and returns it.
func writeCrate(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		p := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// jaccard of two fingerprints' bottom-k sketches (exact here, sets are small).
func jaccard(a, b *protocol.CodeFingerprint) float64 {
	if a == nil || b == nil {
		return 0
	}
	sa := map[string]struct{}{}
	for _, x := range a.M {
		sa[x] = struct{}{}
	}
	inter, union := 0, len(sa)
	for _, x := range b.M {
		if _, ok := sa[x]; ok {
			inter++
		} else {
			union++
		}
	}
	if union == 0 {
		return 0
	}
	return float64(inter) / float64(union)
}

const origCrate = `
fn add(a: i64, b: i64) -> i64 {
    let sum = a + b;
    sum * 2
}

fn greet(name: &str) -> String {
    let msg = format!("hello {}", name);
    msg
}

struct Point { x: i64, y: i64 }

impl Point {
    fn norm(&self) -> i64 {
        self.x * self.x + self.y * self.y
    }
}
`

func TestFromDir_ShapeAndDeterminism(t *testing.T) {
	ctx := context.Background()
	dir := writeCrate(t, map[string]string{"src/lib.rs": origCrate})
	fp := FromDir(ctx, dir)
	if fp == nil {
		t.Fatal("expected a fingerprint")
	}
	if fp.V != Version || fp.K != minhashK {
		t.Fatalf("bad header: %+v", fp)
	}
	if fp.Card == 0 || len(fp.M) == 0 {
		t.Fatalf("empty sketch: %+v", fp)
	}
	// Deterministic.
	fp2 := FromDir(ctx, writeCrate(t, map[string]string{"src/lib.rs": origCrate}))
	if jaccard(fp, fp2) != 1.0 {
		t.Fatalf("non-deterministic: %v", jaccard(fp, fp2))
	}
}

func TestFromDir_IdentifierRenameInvisible(t *testing.T) {
	// Same program, every identifier renamed. The structural stream is identical.
	renamed := `
fn compute(p: i64, q: i64) -> i64 {
    let total = p + q;
    total * 2
}

fn hail(who: &str) -> String {
    let text = format!("hello {}", who);
    text
}

struct Coord { u: i64, v: i64 }

impl Coord {
    fn magnitude(&self) -> i64 {
        self.u * self.u + self.v * self.v
    }
}
`
	ctx := context.Background()
	a := FromDir(ctx, writeCrate(t, map[string]string{"src/lib.rs": origCrate}))
	b := FromDir(ctx, writeCrate(t, map[string]string{"src/lib.rs": renamed}))
	if j := jaccard(a, b); j != 1.0 {
		t.Fatalf("identifier rename should be invisible, jaccard=%v", j)
	}
}

func TestFromDir_ReformatInvisible(t *testing.T) {
	// Same program run through a formatter: collapsed whitespace, different
	// indentation, braces moved. Structural stream unchanged.
	reformatted := "fn add(a:i64,b:i64)->i64{let sum=a+b;sum*2}\n" +
		"fn greet(name:&str)->String{let msg=format!(\"hello {}\",name);msg}\n" +
		"struct Point{x:i64,y:i64}\n" +
		"impl Point{fn norm(&self)->i64{self.x*self.x+self.y*self.y}}\n"
	ctx := context.Background()
	a := FromDir(ctx, writeCrate(t, map[string]string{"src/lib.rs": origCrate}))
	b := FromDir(ctx, writeCrate(t, map[string]string{"src/lib.rs": reformatted}))
	if j := jaccard(a, b); j != 1.0 {
		t.Fatalf("reformatting should be invisible, jaccard=%v", j)
	}
}

func TestFromDir_FileRenameAndReorderInvisible(t *testing.T) {
	ctx := context.Background()
	base := FromDir(ctx, writeCrate(t, map[string]string{
		"src/a.rs": "fn one(x: i64) -> i64 { x + 1 }",
		"src/b.rs": "fn two(y: i64) -> i64 { y * 3 }",
	}))
	// Same two functions, files renamed.
	renamed := FromDir(ctx, writeCrate(t, map[string]string{
		"src/zzz.rs": "fn two(y: i64) -> i64 { y * 3 }",
		"src/qqq.rs": "fn one(x: i64) -> i64 { x + 1 }",
	}))
	if j := jaccard(base, renamed); j != 1.0 {
		t.Fatalf("file rename/reorder should be invisible, jaccard=%v", j)
	}
}

func TestFromDir_DistinctCratesLow(t *testing.T) {
	ctx := context.Background()
	a := FromDir(ctx, writeCrate(t, map[string]string{"src/lib.rs": origCrate}))
	other := `
fn main() {
    for i in 0..10 {
        println!("{}", i);
    }
    let v: Vec<i64> = vec![1, 2, 3];
    match v.len() {
        0 => println!("empty"),
        _ => println!("nonempty"),
    }
}
`
	b := FromDir(ctx, writeCrate(t, map[string]string{"src/main.rs": other}))
	if j := jaccard(a, b); j > 0.5 {
		t.Fatalf("distinct crates should be low, jaccard=%v", j)
	}
}

func TestFromDir_DeepNestingDoesNotCrash(t *testing.T) {
	// Regression: a millions-deep nested expression used to be walked recursively
	// and blew the goroutine stack — a `fatal error: stack overflow` that recover
	// cannot catch, killing the whole scorer process. The walk is now iterative and
	// bounded by maxNodesPerFile, so this must return (nil or a sketch) rather than
	// crash the test binary. The file stays just under maxFileBytes so readCapped
	// accepts it; its depth is well past the old recursive crash threshold.
	const depth = 3_000_000 // ~6 MB of parens, < 8 MiB cap, > recursion crash point
	var b strings.Builder
	b.Grow(2*depth + 32)
	b.WriteString("fn f() -> i64 {\n")
	b.WriteString(strings.Repeat("(", depth))
	b.WriteString("0")
	b.WriteString(strings.Repeat(")", depth))
	b.WriteString("\n}\n")
	dir := writeCrate(t, map[string]string{"src/deep.rs": b.String()})
	// The assertion is simply that this returns; a crash aborts the test binary.
	_ = FromDir(context.Background(), dir)
}

func TestFromDir_CancelledCtxReturnsNil(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // already cancelled before the walk starts
	dir := writeCrate(t, map[string]string{"src/lib.rs": origCrate})
	if fp := FromDir(ctx, dir); fp != nil {
		t.Fatalf("cancelled ctx should yield nil, got %+v", fp)
	}
}

func TestFromDir_ConcurrentRaceFree(t *testing.T) {
	// FromDir is documented as pure + deterministic; run it concurrently on the same
	// crate so `go test -race` exercises the shared-nothing claim (each call builds
	// its own parser + maps). Every result must equal the single-threaded sketch.
	ctx := context.Background()
	dir := writeCrate(t, map[string]string{"src/lib.rs": origCrate})
	want := FromDir(ctx, dir)
	if want == nil {
		t.Fatal("expected a fingerprint")
	}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if got := FromDir(ctx, dir); jaccard(want, got) != 1.0 {
				t.Errorf("concurrent result diverged: %v", jaccard(want, got))
			}
		}()
	}
	wg.Wait()
}

func TestFromDir_NoSupportedSourceReturnsNil(t *testing.T) {
	ctx := context.Background()
	dir := writeCrate(t, map[string]string{
		"README.md":  "# not rust",
		"Cargo.toml": "[package]\nname = \"x\"",
	})
	if fp := FromDir(ctx, dir); fp != nil {
		t.Fatalf("expected nil without supported source, got %+v", fp)
	}
	if fp := FromDir(ctx, t.TempDir()); fp != nil {
		t.Fatalf("expected nil for empty dir, got %+v", fp)
	}
}

func TestFromDir_GenericLanguagesAreFingerprintable(t *testing.T) {
	for name, source := range map[string]string{
		"python/app.py": `
async def answer(request):
    records = await search(request.query)
    return {"answer": records[0].value}
`,
		"typescript/app.ts": `
export async function answer(request: Request): Promise<Response> {
  const records = await search(request.query)
  return { answer: records[0].value }
}
`,
		"go/main.go": `
package main
func answer(request Request) Response {
    records := search(request.Query)
    return Response{Answer: records[0].Value}
}
`,
	} {
		t.Run(name, func(t *testing.T) {
			if fp := FromDir(context.Background(), writeCrate(t, map[string]string{name: source})); fp == nil || fp.Card == 0 {
				t.Fatalf("%s source did not produce a structural fingerprint: %+v", name, fp)
			}
		})
	}
}

func TestFromDir_GenericIdentifierAndLiteralChangesAreInvisible(t *testing.T) {
	original := `
async def answer(request):
    records = await search(request.query)
    if len(records) > 2:
        return {"answer": records[0].value}
    return {"answer": "missing"}
`
	renamed := `
async def resolve(payload):
    matches = await lookup(payload.text)
    if len(matches) > 99:
        return {"result": matches[0].content}
    return {"result": "unknown"}
`
	a := FromDir(context.Background(), writeCrate(t, map[string]string{"app.py": original}))
	b := FromDir(context.Background(), writeCrate(t, map[string]string{"service.py": renamed}))
	if j := jaccard(a, b); j != 1.0 {
		t.Fatalf("generic identifier/literal changes should be invisible, jaccard=%v", j)
	}
}

func TestFromDir_GenericDistinctProgramsDiffer(t *testing.T) {
	a := FromDir(context.Background(), writeCrate(t, map[string]string{"app.py": `
def answer(request):
    records = search(request.query)
    return records[0]
`}))
	b := FromDir(context.Background(), writeCrate(t, map[string]string{"app.py": `
class Queue:
    def run(self):
        while True:
            try:
                yield self.items.pop()
            except IndexError:
                break
`}))
	if j := jaccard(a, b); j > 0.5 {
		t.Fatalf("distinct generic programs should differ, jaccard=%v", j)
	}
}
