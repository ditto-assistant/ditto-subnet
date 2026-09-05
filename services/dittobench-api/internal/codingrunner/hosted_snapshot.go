package codingrunner

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"slices"
	"strings"
	"time"
)

var ErrHostedSnapshot = errors.New("hosted snapshot verification failed")

// HostedSnapshot contains private source bytes. It is never a validator receipt.
type HostedSnapshot struct {
	Bundle        []byte
	Identity      BundleIdentity
	CapsuleSHA256 string
}

func (HostedSnapshot) MarshalJSON() ([]byte, error) {
	return nil, errors.New("hosted snapshots cannot be serialized as diagnostics")
}
func (HostedSnapshot) String() string             { return "HostedSnapshot{private=true}" }
func (value HostedSnapshot) GoString() string     { return value.String() }
func (value HostedSnapshot) LogValue() slog.Value { return slog.StringValue(value.String()) }

type snapshotFile struct {
	Mode      uint32 `json:"mode"`
	Path      string `json:"path"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}

type snapshotManifest struct {
	Schema              string         `json:"schema"`
	Files               []snapshotFile `json:"files"`
	SourceTreeSHA256    string         `json:"source_tree_sha256"`
	SnapshotTreeSHA256  string         `json:"snapshot_tree_sha256"`
	ExcludedRootEntries []string       `json:"excluded_root_entries"`
}

type snapshotEntry struct {
	header tar.Header
	body   []byte
}

// CompileHostedSnapshot verifies an already-authorized plaintext capsule and
// projects only workspace files into the runner's flat tar format. Its expected
// digest must come from the verified private payload, not the candidate. The
// supplied manifest/paths and the outer manifest.json never reach the harness.
// This does not fetch, decrypt, authorize or execute a private task.
func CompileHostedSnapshot(ctx context.Context, capsule []byte, expectedSHA256 string, limits Limits) (HostedSnapshot, error) {
	result, err := compileHostedSnapshot(ctx, capsule, expectedSHA256, limits)
	if err != nil {
		return HostedSnapshot{}, ErrHostedSnapshot
	}
	return result, nil
}

func compileHostedSnapshot(ctx context.Context, capsule []byte, expectedSHA256 string, limits Limits) (HostedSnapshot, error) {
	if ctx == nil || ctx.Err() != nil || limits.Validate() != nil || !isLowerSHA256(expectedSHA256) ||
		len(capsule) == 0 || len(capsule) > 128<<20 || int64(len(capsule)) > limits.MaxBundleBytes || len(capsule)%10240 != 0 || sha256Hex(capsule) != expectedSHA256 {
		return HostedSnapshot{}, ErrHostedSnapshot
	}
	source := bytes.NewReader(capsule)
	archive := tar.NewReader(source)
	entries := map[string]snapshotEntry{}
	var total int64
	for {
		if ctx.Err() != nil {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		header, err := archive.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		if _, duplicate := entries[header.Name]; duplicate {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		if (header.Typeflag != tar.TypeReg && header.Typeflag != tar.TypeRegA) || header.Uid != 0 || header.Gid != 0 ||
			header.Uname != "" || header.Gname != "" || header.ModTime.Unix() != 0 || header.Size < 0 || len(entries) > limits.MaxEntries {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		for name := range header.PAXRecords {
			if name != "path" {
				return HostedSnapshot{}, ErrHostedSnapshot
			}
		}
		if header.Name == "manifest.json" {
			if header.Size == 0 || header.Size > 8<<20 || (header.Mode != 0600 && header.Mode != 0644) {
				return HostedSnapshot{}, ErrHostedSnapshot
			}
		} else {
			if !strings.HasPrefix(header.Name, "workspace/") || !safeSnapshotPath(strings.TrimPrefix(header.Name, "workspace/")) ||
				(header.Mode != 0644 && header.Mode != 0755) || header.Size > limits.MaxFileBytes || total > limits.MaxWorkspaceBytes-header.Size {
				return HostedSnapshot{}, ErrHostedSnapshot
			}
			total += header.Size
		}
		body, err := io.ReadAll(io.LimitReader(archive, header.Size+1))
		if err != nil || int64(len(body)) != header.Size {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		entries[header.Name] = snapshotEntry{*header, body}
	}
	for _, value := range capsule[len(capsule)-source.Len():] {
		if value != 0 {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
	}
	entry, ok := entries["manifest.json"]
	if !ok {
		return HostedSnapshot{}, ErrHostedSnapshot
	}
	var manifest snapshotManifest
	if json.Unmarshal(entry.body, &manifest) != nil || manifest.Schema != "dittobench-coding-sanitized-snapshot-v2" ||
		manifest.Files == nil || manifest.ExcludedRootEntries == nil || len(manifest.Files) > limits.MaxEntries || len(manifest.Files)+1 != len(entries) {
		return HostedSnapshot{}, ErrHostedSnapshot
	}
	canonical, err := canonicalStruct(manifest)
	if err != nil || !bytes.Equal(canonical, entry.body) {
		return HostedSnapshot{}, ErrHostedSnapshot
	}
	previous := ""
	for _, name := range manifest.ExcludedRootEntries {
		if (name != ".git" && name != ".github") || name <= previous {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		previous = name
	}
	tree, err := canonicalStruct(manifest.Files)
	if err != nil || sha256Hex(tree) != manifest.SourceTreeSHA256 || manifest.SourceTreeSHA256 != manifest.SnapshotTreeSHA256 {
		return HostedSnapshot{}, ErrHostedSnapshot
	}
	var flat bytes.Buffer
	writer := tar.NewWriter(&flat)
	previous = ""
	for _, file := range manifest.Files {
		if ctx.Err() != nil || !safeSnapshotPath(file.Path) || !isLowerSHA256(file.SHA256) ||
			(previous != "" && slices.Compare(strings.Split(previous, "/"), strings.Split(file.Path, "/")) >= 0) {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		previous = file.Path
		entry, ok := entries["workspace/"+file.Path]
		if !ok || uint32(entry.header.Mode) != file.Mode || entry.header.Size != file.SizeBytes || sha256Hex(entry.body) != file.SHA256 {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		if err := writer.WriteHeader(&tar.Header{Name: file.Path, Typeflag: tar.TypeReg, Mode: int64(file.Mode), Size: file.SizeBytes, ModTime: time.Unix(0, 0), Format: tar.FormatPAX}); err != nil {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
		if _, err := writer.Write(entry.body); err != nil || int64(flat.Len()) > limits.MaxBundleBytes {
			return HostedSnapshot{}, ErrHostedSnapshot
		}
	}
	if writer.Close() != nil || int64(flat.Len()) > limits.MaxBundleBytes {
		return HostedSnapshot{}, ErrHostedSnapshot
	}
	identity, err := InspectBundle(ctx, bytes.NewReader(flat.Bytes()), limits)
	if err != nil {
		return HostedSnapshot{}, ErrHostedSnapshot
	}
	return HostedSnapshot{Bundle: flat.Bytes(), Identity: identity, CapsuleSHA256: expectedSHA256}, nil
}

func safeSnapshotPath(path string) bool {
	if _, err := safePath(path, false); err != nil {
		return false
	}
	for _, part := range strings.Split(path, "/") {
		if part == ".env" || strings.HasPrefix(part, ".env.") {
			return false
		}
		switch part {
		case ".git", ".github", ".venv", "__pycache__", ".pytest_cache", "node_modules", "target", ".idea", ".vscode":
			return false
		}
	}
	return true
}
