// Package astfp computes a structural fingerprint of a submitted harness. It
// uses tree-sitter's Rust AST when Rust is present and a language-aware,
// identifier/literal-neutral token structure for other source languages. It is
// the structural counterpart to the platform's lexical
// content fingerprint (ditto-platform ditto/api_server/fingerprint.py): where the
// lexical signal survives reformatting and localized edits, this survives
// identifier renaming and reformatting because it hashes only the *shape* of the
// parse tree, never the text of identifiers or literals.
//
// How it works. Each .rs file under the extracted harness is parsed with
// tree-sitter-rust; a pre-order walk emits the sequence of *named node types*
// (e.g. function_item, let_declaration, binary_expression) — the leaves that
// carry a name or a literal contribute only their node type, never their text,
// so renaming every variable leaves the stream identical. The per-file streams
// are cut into overlapping k-node shingles, hashed, and unioned across files, so
// renaming/reordering files is invisible and a localized edit disturbs only the
// few shingles spanning it.
//
// Storage is a bottom-k MinHash (KMV) sketch in exactly the wire shape the
// platform already compares (`{v,k,card,m}`), so the platform reuses its own
// similarity estimator on the structural channel. This fingerprint travels in the
// ScoreReport as advisory (unsigned) moderation metadata; the platform's anti-copy
// gate holds a cross-miner structural near-duplicate for human review.
//
// Other common compiled and interpreted source files use a bounded generic
// tokenizer that retains keywords/operators while replacing identifiers and
// literals. This keeps renamed Python, TypeScript, Go, Java, C/C++, C#, Ruby,
// PHP, Swift, Kotlin, Scala, Elixir/Erlang, Lua, Dart, Zig, shell, and similar
// harnesses comparable without making any language an admission requirement.
// Everything is pure + deterministic: the same source tree yields the same
// sketch.
package astfp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	sitter "github.com/smacker/go-tree-sitter"
	"github.com/smacker/go-tree-sitter/rust"
)

const (
	// Version stamps the sketch format. It must match the platform sketch format
	// version so the two sides compare (see the platform's content_similarity,
	// which requires equal versions). Bump on any change to normalization,
	// shingling, or the sketch shape.
	Version = 1
	// A shingle is this many consecutive named node types. Small enough that a
	// localized edit disturbs few shingles; large enough to be distinctive.
	shingleNodes         = 6
	genericShingleTokens = 10
	// Bottom-k sketch budget — fixed-size summary, exact when the shingle set is
	// smaller than k (the common case).
	minhashK = 256
	// Width of each shingle hash as zero-padded hex (64 bits): fixed width so the
	// hex strings sort in the same order as the integers they encode.
	hashHex = 16

	// Work/DoS guards, mirroring the sandbox extractor's posture. The crate is
	// already extracted from a size-capped tarball, but a hostile tree can still
	// be pathological, so the walk is bounded independently.
	maxFiles     = 5000
	maxFileBytes = 8 * 1024 * 1024
	maxShingles  = 500_000
	// Per-file cap on named AST nodes walked. Bounds CPU + memory on a pathological
	// tree (e.g. a millions-deep nested expression) and, with the iterative walk,
	// prevents the walk from becoming a runaway. Well above any real source file
	// (which is KBs); a file that exceeds it is skipped (fail-open).
	maxNodesPerFile = 2_000_000
	// How often the walk checks ctx for cancellation (the build timeout).
	ctxCheckEvery = 8192
)

// FromDir walks the extracted harness at dir and returns its structural
// fingerprint (a protocol.CodeFingerprint sketch), or nil when it has no
// supported parseable source or trips a guard.
//
// It never returns an error: fingerprinting is a best-effort moderation signal
// layered on an already-verified, about-to-be-scored submission, so a crate that
// defeats it simply yields no structural signal (the platform then relies on its
// lexical + size signals). A cancelled ctx aborts the walk and yields nil.
func FromDir(ctx context.Context, dir string) *protocol.CodeFingerprint {
	parser := sitter.NewParser()
	parser.SetLanguage(rust.GetLanguage())

	shingles := map[string]struct{}{}
	files := 0
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // skip unreadable entries rather than abort the whole walk
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if d.IsDir() {
			return nil
		}
		kind, supported := sourceKind(path)
		if !supported {
			return nil
		}
		files++
		if files > maxFiles {
			return fs.SkipAll
		}
		src, rerr := readCapped(path, maxFileBytes)
		if rerr != nil {
			return nil
		}
		fileHashes := genericFileShingles(kind, src)
		if kind == "rust" {
			fileHashes = fileShingles(ctx, parser, src)
		}
		for _, s := range fileHashes {
			shingles[s] = struct{}{}
			if len(shingles) > maxShingles {
				return fs.SkipAll
			}
		}
		return nil
	})
	if err != nil || len(shingles) == 0 {
		return nil
	}
	return sketch(shingles)
}

var sourceKinds = map[string]string{
	".rs": "rust", ".py": "python", ".pyw": "python",
	".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
	".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
	".go": "go", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
	".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hh": "cpp", ".hpp": "cpp",
	".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
	".scala": "scala", ".sc": "scala", ".ex": "elixir", ".exs": "elixir",
	".erl": "erlang", ".hrl": "erlang", ".fs": "fsharp", ".fsx": "fsharp",
	".lua": "lua", ".dart": "dart", ".zig": "zig",
	".sh": "shell", ".bash": "shell", ".zsh": "shell",
	".vue": "web", ".svelte": "web",
}

func sourceKind(path string) (string, bool) {
	kind, ok := sourceKinds[strings.ToLower(filepath.Ext(path))]
	return kind, ok
}

var structuralKeywords = map[string]struct{}{
	"as": {}, "async": {}, "await": {}, "break": {}, "case": {}, "catch": {},
	"class": {}, "const": {}, "continue": {}, "def": {}, "defer": {}, "do": {},
	"else": {}, "elif": {}, "enum": {}, "except": {}, "export": {}, "extends": {},
	"false": {}, "finally": {}, "fn": {}, "for": {}, "foreach": {}, "from": {},
	"func": {}, "function": {}, "go": {}, "if": {}, "implements": {}, "import": {},
	"in": {}, "interface": {}, "let": {}, "loop": {}, "match": {}, "mod": {},
	"new": {}, "nil": {}, "none": {}, "null": {}, "package": {}, "pass": {},
	"private": {}, "protected": {}, "public": {}, "raise": {}, "return": {},
	"select": {}, "self": {}, "static": {}, "struct": {}, "super": {}, "switch": {},
	"this": {}, "throw": {}, "trait": {}, "true": {}, "try": {}, "type": {},
	"typeof": {}, "union": {}, "unsafe": {}, "use": {}, "var": {}, "while": {},
	"with": {}, "yield": {},
}

func genericFileShingles(kind string, src []byte) []string {
	tokens := genericTokens(kind, []rune(string(src)))
	if len(tokens) == 0 {
		return nil
	}
	if len(tokens) < genericShingleTokens {
		return []string{hashShingle("generic:" + kind + ":" + strings.Join(tokens, " "))}
	}
	out := make([]string, 0, len(tokens)-genericShingleTokens+1)
	for i := 0; i+genericShingleTokens <= len(tokens); i++ {
		out = append(out, hashShingle("generic:"+kind+":"+strings.Join(tokens[i:i+genericShingleTokens], " ")))
	}
	return out
}

func genericTokens(kind string, src []rune) []string {
	tokens := make([]string, 0, min(len(src)/3, maxNodesPerFile))
	hashComment := kind == "python" || kind == "ruby" || kind == "shell" || kind == "php"
	for i := 0; i < len(src) && len(tokens) < maxNodesPerFile; {
		r := src[i]
		if unicode.IsSpace(r) {
			i++
			continue
		}
		if hashComment && r == '#' {
			for i < len(src) && src[i] != '\n' {
				i++
			}
			continue
		}
		if r == '/' && i+1 < len(src) && src[i+1] == '/' {
			i += 2
			for i < len(src) && src[i] != '\n' {
				i++
			}
			continue
		}
		if r == '/' && i+1 < len(src) && src[i+1] == '*' {
			i += 2
			for i+1 < len(src) && !(src[i] == '*' && src[i+1] == '/') {
				i++
			}
			if i+1 < len(src) {
				i += 2
			}
			continue
		}
		if r == '\'' || r == '"' || r == '`' {
			quote := r
			i++
			for i < len(src) {
				if src[i] == '\\' {
					i += 2
					continue
				}
				if src[i] == quote {
					i++
					break
				}
				i++
			}
			tokens = append(tokens, "literal")
			continue
		}
		if unicode.IsDigit(r) {
			i++
			for i < len(src) && (unicode.IsDigit(src[i]) || unicode.IsLetter(src[i]) || strings.ContainsRune("._", src[i])) {
				i++
			}
			tokens = append(tokens, "number")
			continue
		}
		if r == '_' || unicode.IsLetter(r) {
			start := i
			i++
			for i < len(src) && (src[i] == '_' || unicode.IsLetter(src[i]) || unicode.IsDigit(src[i])) {
				i++
			}
			word := strings.ToLower(string(src[start:i]))
			if _, keyword := structuralKeywords[word]; keyword {
				tokens = append(tokens, word)
			} else {
				tokens = append(tokens, "identifier")
			}
			continue
		}
		if i+1 < len(src) {
			pair := string(src[i : i+2])
			if strings.Contains(" == != <= >= -> => :: := && || ++ -- += -= *= /= ** ?? ?. << >> ", " "+pair+" ") {
				tokens = append(tokens, pair)
				i += 2
				continue
			}
		}
		tokens = append(tokens, string(r))
		i++
	}
	return tokens
}

// readCapped reads the whole file up to max bytes; a file larger than the cap is
// skipped entirely (returns an error) so a truncated read can't produce a hash a
// smaller honest file could collide with. It reads via io.ReadAll over a capped
// LimitReader (+1 byte to detect the over-cap case) rather than a single Read,
// because os.File.Read may return a short read on a large file — which would
// silently truncate the content and make the fingerprint non-deterministic.
func readCapped(path string, max int) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, int64(max)+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > max {
		return nil, fs.ErrInvalid
	}
	return raw, nil
}

// fileShingles parses one file and returns the hashed k-node shingles of its
// named-node-type stream — the pre-order sequence of named node *types* (never
// the source text, so identifier/literal renaming is invisible; anonymous
// punctuation/keyword nodes are skipped). Fewer than shingleNodes named nodes
// yields one shingle of the whole stream (nil for an empty/unparseable file).
//
// The walk is ITERATIVE, not recursive: a deeply-nested expression would blow the
// goroutine stack under recursion, and a stack overflow is an unrecoverable Go
// `fatal error` (recover cannot catch it) that would crash the whole scorer. It
// also honors ctx (the build timeout) and bails past maxNodesPerFile so a
// pathological tree cannot burn unbounded CPU/memory. Sliding a k-wide window over
// the type stream yields the same shingles as hashing every k-node run, in O(k).
func fileShingles(ctx context.Context, parser *sitter.Parser, src []byte) []string {
	tree, err := parser.ParseCtx(ctx, nil, src)
	if err != nil || tree == nil {
		return nil
	}
	defer tree.Close()
	root := tree.RootNode()
	if root == nil {
		return nil
	}

	var out []string
	window := make([]string, 0, shingleNodes)
	stack := []*sitter.Node{root}
	nodes := 0
	for len(stack) > 0 {
		n := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if n.IsNamed() {
			nodes++
			if nodes > maxNodesPerFile {
				return nil // pathological tree — skip this file (fail-open)
			}
			if nodes%ctxCheckEvery == 0 && ctx.Err() != nil {
				return nil
			}
			t := n.Type()
			if len(window) < shingleNodes {
				window = append(window, t)
				if len(window) == shingleNodes {
					out = append(out, hashShingle(strings.Join(window, " ")))
				}
			} else {
				copy(window, window[1:]) // slide left in place (no realloc)
				window[shingleNodes-1] = t
				out = append(out, hashShingle(strings.Join(window, " ")))
			}
		}
		// Push named children in reverse so they pop in forward (pre-order) order.
		for i := int(n.NamedChildCount()) - 1; i >= 0; i-- {
			stack = append(stack, n.NamedChild(i))
		}
	}
	if len(out) > 0 {
		return out
	}
	if len(window) == 0 {
		return nil // no named nodes at all
	}
	// Fewer than shingleNodes named nodes: one whole-file shingle (matches the
	// sliding-window result when the stream length == k).
	return []string{hashShingle(strings.Join(window, " "))}
}

func hashShingle(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])[:hashHex]
}

// sketch builds the bottom-k MinHash from a shingle-hash set.
func sketch(shingles map[string]struct{}) *protocol.CodeFingerprint {
	all := make([]string, 0, len(shingles))
	for s := range shingles {
		all = append(all, s)
	}
	sort.Strings(all)
	m := all
	if len(m) > minhashK {
		m = m[:minhashK]
	}
	return &protocol.CodeFingerprint{V: Version, K: minhashK, Card: len(shingles), M: m}
}
