package challenge

import (
	"archive/tar"
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Source archive limits. A generator is small Go source; these bounds keep an
// upload cheap to store, inspect, and build.
const (
	MaxArchiveBytes     = 4 << 20
	MaxSourceBytes      = 32 << 20
	MaxSourceFiles      = 4096
	MaxSourcePathLength = 240
)

// BuilderGoVersion is the pinned toolchain the scorer builds generators with.
// A generator's go.mod `go` directive must not exceed it (the build runs with
// GOTOOLCHAIN=local, so a newer directive fails instead of downloading).
const BuilderGoVersion = "1.27.3"

// ArchiveInfo describes a structurally valid generator source archive.
type ArchiveInfo struct {
	// SHA256 is the Digest of the exact archive bytes: what the miner signs.
	SHA256 string `json:"sha256"`
	// TreeSHA256 identifies the source tree independently of tar ordering,
	// timestamps, permissions, and compression, so re-packing the same files
	// cannot pass as a new generator. See TreeDigest.
	TreeSHA256 string `json:"tree_sha256"`
	Module     string `json:"module"`
	GoVersion  string `json:"go_version"`
	Files      int    `json:"files"`
	Bytes      int64  `json:"bytes"`
	Vendored   bool   `json:"vendored"`
}

// SourceFile is one regular file in an archive.
type SourceFile struct {
	Path string
	Data []byte
}

var (
	moduleLine = regexp.MustCompile(`^module\s+("?)([^\s"]+)("?)\s*(//.*)?$`)
	goLine     = regexp.MustCompile(`^go\s+([0-9]+\.[0-9]+(\.[0-9]+)?)\s*(//.*)?$`)
	requireTok = regexp.MustCompile(`^require(\s|\()`)
)

// InspectArchive checks a generator source archive (gzip-compressed tar) and
// returns its identity and files. Only regular files and directories with
// clean relative paths are allowed: no links, devices, absolute paths, `..`,
// or duplicates. The root must hold go.mod and at least one .go file, the go
// directive must not exceed BuilderGoVersion, and a module with requirements
// must vendor them (the build has no network).
func InspectArchive(raw []byte) (ArchiveInfo, []SourceFile, error) {
	if len(raw) > MaxArchiveBytes {
		return ArchiveInfo{}, nil, fmt.Errorf("source archive exceeds %d bytes", MaxArchiveBytes)
	}
	gz, err := gzip.NewReader(bytes.NewReader(raw))
	if err != nil {
		return ArchiveInfo{}, nil, fmt.Errorf("source archive is not gzip: %w", err)
	}
	tr := tar.NewReader(gz)
	var files []SourceFile
	seen := map[string]bool{}
	var total int64
	entries := 0
	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return ArchiveInfo{}, nil, fmt.Errorf("source archive: %w", err)
		}
		if hdr.Typeflag == tar.TypeXGlobalHeader {
			continue
		}
		entries++
		if hdr.Typeflag == tar.TypeDir && (hdr.Name == "." || hdr.Name == "./" || hdr.Name == "") {
			continue // the root itself, as `tar -C dir .` writes it
		}
		if entries > MaxSourceFiles {
			return ArchiveInfo{}, nil, fmt.Errorf("source archive has more than %d entries", MaxSourceFiles)
		}
		name, err := cleanArchivePath(hdr.Name)
		if err != nil {
			return ArchiveInfo{}, nil, err
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			continue
		case tar.TypeReg:
		default:
			return ArchiveInfo{}, nil, fmt.Errorf("source archive entry %q is not a regular file or directory", name)
		}
		if seen[name] {
			return ArchiveInfo{}, nil, fmt.Errorf("source archive repeats %q", name)
		}
		seen[name] = true
		if hdr.Size < 0 || total+hdr.Size > MaxSourceBytes {
			return ArchiveInfo{}, nil, fmt.Errorf("source archive expands past %d bytes", MaxSourceBytes)
		}
		data, err := io.ReadAll(io.LimitReader(tr, hdr.Size+1))
		if err != nil {
			return ArchiveInfo{}, nil, fmt.Errorf("source archive entry %q: %w", name, err)
		}
		if int64(len(data)) != hdr.Size {
			return ArchiveInfo{}, nil, fmt.Errorf("source archive entry %q is truncated", name)
		}
		total += hdr.Size
		files = append(files, SourceFile{Path: name, Data: data})
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })

	info := ArchiveInfo{SHA256: Digest(raw), TreeSHA256: TreeDigest(files), Files: len(files), Bytes: total}
	var gomod []byte
	hasRootGo := false
	for _, f := range files {
		switch {
		case f.Path == "go.mod":
			gomod = f.Data
		case f.Path == "vendor/modules.txt":
			info.Vendored = true
		case !strings.Contains(f.Path, "/") && strings.HasSuffix(f.Path, ".go"):
			hasRootGo = true
		}
	}
	if gomod == nil {
		return ArchiveInfo{}, nil, errors.New("source archive must have go.mod at its root")
	}
	if !hasRootGo {
		return ArchiveInfo{}, nil, errors.New("source archive must have the main package's .go files at its root")
	}
	module, goVersion, requires := parseGoMod(gomod)
	if module == "" {
		return ArchiveInfo{}, nil, errors.New("go.mod must declare a module path")
	}
	if goVersion == "" {
		return ArchiveInfo{}, nil, errors.New("go.mod must declare a go version")
	}
	if compareGoVersions(goVersion, BuilderGoVersion) > 0 {
		return ArchiveInfo{}, nil, fmt.Errorf("go.mod requires go %s; the builder runs go %s", goVersion, BuilderGoVersion)
	}
	if requires && !info.Vendored {
		return ArchiveInfo{}, nil, errors.New("go.mod has requirements but vendor/modules.txt is missing; run `go mod vendor` (the build has no network)")
	}
	info.Module, info.GoVersion = module, goVersion
	return info, files, nil
}

// ExtractArchive inspects a source archive and writes its files under dir.
func ExtractArchive(raw []byte, dir string) (ArchiveInfo, error) {
	info, files, err := InspectArchive(raw)
	if err != nil {
		return ArchiveInfo{}, err
	}
	for _, f := range files {
		dst := filepath.Join(dir, filepath.FromSlash(f.Path))
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return ArchiveInfo{}, err
		}
		if err := os.WriteFile(dst, f.Data, 0o644); err != nil {
			return ArchiveInfo{}, err
		}
	}
	return info, nil
}

// TreeDigest is the content identity of a source tree: the SHA-256 over every
// regular file in path order of `path NUL hex(sha256(content)) LF`. Platform's
// upload check computes the same digest (apps/platform/ditto/generator_archive.py).
func TreeDigest(files []SourceFile) string {
	sorted := append([]SourceFile(nil), files...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Path < sorted[j].Path })
	h := sha256.New()
	for _, f := range sorted {
		sum := sha256.Sum256(f.Data)
		h.Write([]byte(f.Path))
		h.Write([]byte{0})
		h.Write([]byte(hex.EncodeToString(sum[:])))
		h.Write([]byte{'\n'})
	}
	return hex.EncodeToString(h.Sum(nil))
}

func cleanArchivePath(name string) (string, error) {
	trimmed := strings.TrimSuffix(strings.TrimPrefix(name, "./"), "/")
	switch {
	case trimmed == "" || trimmed == ".":
		return "", fmt.Errorf("source archive entry %q has an empty path", name)
	case len(trimmed) > MaxSourcePathLength:
		return "", fmt.Errorf("source archive entry %q path is longer than %d", name, MaxSourcePathLength)
	case strings.ContainsAny(trimmed, "\\\x00") || strings.HasPrefix(trimmed, "/"):
		return "", fmt.Errorf("source archive entry %q path must be relative with forward slashes", name)
	case path.Clean(trimmed) != trimmed:
		return "", fmt.Errorf("source archive entry %q path is not clean", name)
	}
	for _, part := range strings.Split(trimmed, "/") {
		if part == ".." || part == "." {
			return "", fmt.Errorf("source archive entry %q path escapes the root", name)
		}
	}
	return trimmed, nil
}

func parseGoMod(raw []byte) (module, goVersion string, requires bool) {
	sc := bufio.NewScanner(bytes.NewReader(raw))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if m := moduleLine.FindStringSubmatch(line); m != nil {
			module = m[2]
		} else if m := goLine.FindStringSubmatch(line); m != nil {
			goVersion = m[1]
		} else if requireTok.MatchString(line) {
			requires = true
		}
	}
	return module, goVersion, requires
}

func compareGoVersions(a, b string) int {
	pa, pb := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < 3; i++ {
		var x, y int
		if i < len(pa) {
			x, _ = strconv.Atoi(pa[i])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(pb[i])
		}
		if x != y {
			if x < y {
				return -1
			}
			return 1
		}
	}
	return 0
}

// CollectSource reads a generator directory into archive files: every regular
// file except VCS metadata, editor/OS junk, and build output (`bin/`, `out/`).
func CollectSource(dir string) ([]SourceFile, error) {
	var files []SourceFile
	err := filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(dir, p)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if rel == "." {
			return nil
		}
		base := d.Name()
		if d.IsDir() {
			if base == ".git" || base == ".hg" || base == ".idea" || base == ".vscode" || rel == "bin" || rel == "out" {
				return filepath.SkipDir
			}
			return nil
		}
		if base == ".DS_Store" || strings.HasPrefix(base, "._") {
			return nil
		}
		if !d.Type().IsRegular() {
			return fmt.Errorf("%s is not a regular file; generator sources may not contain links", rel)
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		files = append(files, SourceFile{Path: rel, Data: data})
		return nil
	})
	return files, err
}

// PackArchive builds a deterministic source archive from files (sorted paths,
// zero timestamps, 0644 files), the form the miner CLI uploads. Identical
// trees always produce identical archive bytes.
func PackArchive(files []SourceFile) ([]byte, error) {
	sorted := append([]SourceFile(nil), files...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Path < sorted[j].Path })
	var buf bytes.Buffer
	gz, err := gzip.NewWriterLevel(&buf, gzip.BestCompression)
	if err != nil {
		return nil, err
	}
	tw := tar.NewWriter(gz)
	for _, f := range sorted {
		hdr := &tar.Header{Name: f.Path, Mode: 0o644, Size: int64(len(f.Data)), Typeflag: tar.TypeReg}
		if err := tw.WriteHeader(hdr); err != nil {
			return nil, err
		}
		if _, err := tw.Write(f.Data); err != nil {
			return nil, err
		}
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	if err := gz.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
