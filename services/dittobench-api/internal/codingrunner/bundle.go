package codingrunner

import (
	"archive/tar"
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const initialEventRoot = "0000000000000000000000000000000000000000000000000000000000000000"

type fileState struct {
	sha256 string
	size   int64
	mode   fs.FileMode
	kind   string
}

type treeEntry struct {
	Path      string `json:"path"`
	Type      string `json:"type"`
	Mode      uint32 `json:"mode"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}

func sha256Hex(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}

func isLowerSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func canonicalStruct(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projection any
	if err := decoder.Decode(&projection); err != nil {
		return nil, err
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projection); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}

func openTar(source io.Reader) (*tar.Reader, io.Closer, error) {
	reader := bufio.NewReader(source)
	header, err := reader.Peek(2)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, nil, fmt.Errorf("inspect visible capsule: %w", err)
	}
	if len(header) >= 2 && header[0] == 0x1f && header[1] == 0x8b {
		compressed, err := gzip.NewReader(reader)
		if err != nil {
			return nil, nil, fmt.Errorf("open visible capsule gzip: %w", err)
		}
		return tar.NewReader(compressed), compressed, nil
	}
	return tar.NewReader(reader), io.NopCloser(bytes.NewReader(nil)), nil
}

func extractVisibleBundle(ctx context.Context, root string, source io.Reader, limits Limits) error {
	reader, closer, err := openTar(source)
	if err != nil {
		return err
	}
	defer closer.Close()
	seen := make(map[string]struct{})
	entries := 0
	regularFiles := 0
	var total int64
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		header, nextErr := reader.Next()
		if errors.Is(nextErr, io.EOF) {
			break
		}
		if nextErr != nil {
			return fmt.Errorf("read visible capsule: %w", nextErr)
		}
		relative, pathErr := safePath(strings.TrimSuffix(header.Name, "/"), false)
		if pathErr != nil {
			return fmt.Errorf("visible capsule entry %q: %w", header.Name, pathErr)
		}
		if _, duplicate := seen[relative]; duplicate {
			return fmt.Errorf("visible capsule repeats path %q", relative)
		}
		seen[relative] = struct{}{}
		entries++
		if entries > limits.MaxEntries {
			return errors.New("visible capsule exceeds the signed entry envelope")
		}
		target := filepath.Join(root, filepath.FromSlash(relative))
		switch header.Typeflag {
		case tar.TypeDir:
			if header.Size != 0 {
				return fmt.Errorf("visible capsule directory %q has content", relative)
			}
			if err := mkdirAllExact(root, relative); err != nil {
				return fmt.Errorf("create visible directory: %w", err)
			}
		case tar.TypeReg, tar.TypeRegA:
			regularFiles++
			if header.Size < 0 || header.Size > limits.MaxFileBytes ||
				total > limits.MaxWorkspaceBytes-header.Size {
				return errors.New("visible capsule exceeds the signed file envelope")
			}
			mode := fs.FileMode(header.Mode) & fs.ModePerm
			if mode&0o400 == 0 || mode&0o022 != 0 || header.Mode&^int64(fs.ModePerm) != 0 {
				return fmt.Errorf("visible capsule file %q has an unsafe mode", relative)
			}
			parent := filepath.ToSlash(filepath.Dir(filepath.FromSlash(relative)))
			if err := mkdirAllExact(root, parent); err != nil {
				return fmt.Errorf("create visible parent: %w", err)
			}
			handle, openErr := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
			if openErr != nil {
				return fmt.Errorf("create visible file: %w", openErr)
			}
			if chmodErr := handle.Chmod(mode); chmodErr != nil {
				handle.Close()
				return fmt.Errorf("set visible file mode: %w", chmodErr)
			}
			written, _, copyErr := copyAndHash(ctx, handle, reader, header.Size)
			closeErr := handle.Close()
			if copyErr != nil || written != header.Size {
				return fmt.Errorf("write visible file %q: %w", relative, copyErr)
			}
			if closeErr != nil {
				return fmt.Errorf("close visible file: %w", closeErr)
			}
			total += header.Size
		default:
			return fmt.Errorf("visible capsule entry %q is not a regular file or directory", relative)
		}
	}
	if regularFiles == 0 {
		return errors.New("visible capsule contains no files")
	}
	return nil
}

func mkdirAllExact(root, relative string) error {
	if relative == "." || relative == "" {
		return nil
	}
	current := root
	for _, part := range strings.Split(filepath.ToSlash(relative), "/") {
		current = filepath.Join(current, filepath.FromSlash(part))
		info, err := os.Lstat(current)
		if errors.Is(err, fs.ErrNotExist) {
			if err := os.Mkdir(current, 0o700); err != nil {
				return err
			}
			if err := os.Chmod(current, 0o700); err != nil {
				return err
			}
			continue
		}
		if err != nil {
			return err
		}
		if !info.IsDir() || info.Mode()&fs.ModeSymlink != 0 {
			return errors.New("visible capsule parent is not a real directory")
		}
		if err := os.Chmod(current, 0o700); err != nil {
			return err
		}
	}
	return nil
}

type contextReader struct {
	ctx    context.Context
	source io.Reader
}

func (reader contextReader) Read(buffer []byte) (int, error) {
	if err := reader.ctx.Err(); err != nil {
		return 0, err
	}
	return reader.source.Read(buffer)
}

func stageVisibleBundle(ctx context.Context, source io.Reader, maximum int64) (*os.File, string, error) {
	if source == nil || maximum <= 0 {
		return nil, "", errors.New("visible capsule stream is unavailable")
	}
	handle, err := os.CreateTemp("", ".dittobench-visible-capsule-*")
	if err != nil {
		return nil, "", fmt.Errorf("stage visible capsule: %w", err)
	}
	cleanup := true
	defer func() {
		if cleanup {
			name := handle.Name()
			handle.Close()
			_ = os.Remove(name)
		}
	}()
	if err := handle.Chmod(0o600); err != nil {
		return nil, "", err
	}
	hasher := sha256.New()
	limited := &io.LimitedReader{R: contextReader{ctx: ctx, source: source}, N: maximum + 1}
	written, err := io.Copy(io.MultiWriter(handle, hasher), limited)
	if err != nil {
		return nil, "", fmt.Errorf("read visible capsule stream: %w", err)
	}
	if written == 0 || written > maximum {
		return nil, "", errors.New("visible capsule bytes exceed the signed bundle limit")
	}
	if err := handle.Sync(); err != nil {
		return nil, "", err
	}
	if _, err := handle.Seek(0, io.SeekStart); err != nil {
		return nil, "", err
	}
	cleanup = false
	return handle, hex.EncodeToString(hasher.Sum(nil)), nil
}

func copyAndHash(ctx context.Context, destination io.Writer, source io.Reader, size int64) (int64, string, error) {
	hasher := sha256.New()
	written, err := io.CopyN(io.MultiWriter(destination, hasher), contextReader{ctx: ctx, source: source}, size)
	if err != nil {
		return written, "", err
	}
	if written != size {
		return written, "", errors.New("workspace file changed while being hashed")
	}
	return written, hex.EncodeToString(hasher.Sum(nil)), nil
}

func readBoundedFile(ctx context.Context, filePath string, maximum int64) ([]byte, error) {
	handle, err := os.Open(filePath)
	if err != nil {
		return nil, err
	}
	defer handle.Close()
	limited := &io.LimitedReader{R: contextReader{ctx: ctx, source: handle}, N: maximum + 1}
	body, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > maximum {
		return nil, errors.New("workspace file exceeds its signed limit")
	}
	return body, nil
}

func hashFile(ctx context.Context, filePath string, size int64) (string, error) {
	handle, err := os.Open(filePath)
	if err != nil {
		return "", err
	}
	defer handle.Close()
	written, digest, err := copyAndHash(ctx, io.Discard, handle, size)
	if err != nil {
		return "", err
	}
	if written != size {
		return "", errors.New("workspace file changed while being hashed")
	}
	return digest, nil
}

type workspaceViolation struct {
	code string
	err  error
}

func (violation *workspaceViolation) Error() string { return violation.err.Error() }
func (violation *workspaceViolation) Unwrap() error { return violation.err }

func violation(code, message string, arguments ...any) error {
	return &workspaceViolation{code: code, err: fmt.Errorf(message, arguments...)}
}

func snapshot(ctx context.Context, root string, limits Limits) (map[string]fileState, error) {
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&fs.ModeSymlink != 0 {
		return nil, fmt.Errorf("workspace root is unavailable")
	}
	result := make(map[string]fileState)
	entries := 0
	var total int64
	err = filepath.WalkDir(root, func(current string, entry fs.DirEntry, walkErr error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if walkErr != nil {
			return walkErr
		}
		if current == root {
			return nil
		}
		relativeOS, relErr := filepath.Rel(root, current)
		if relErr != nil {
			return relErr
		}
		relative := filepath.ToSlash(relativeOS)
		if _, pathErr := safePath(relative, false); pathErr != nil {
			return violation("unsafe_path", "workspace contains unsafe path %q", relative)
		}
		entryInfo, infoErr := os.Lstat(current)
		if infoErr != nil {
			return infoErr
		}
		if entryInfo.Mode()&fs.ModeSymlink != 0 {
			return violation("symlink", "workspace contains symlink %q", relative)
		}
		entries++
		if entries > limits.MaxEntries {
			return violation("workspace_limit", "workspace exceeds its signed entry envelope")
		}
		if entryInfo.IsDir() {
			result[relative] = fileState{mode: entryInfo.Mode() & fs.ModePerm, kind: "directory"}
			return nil
		}
		if !entryInfo.Mode().IsRegular() {
			return violation("special_file", "workspace contains special file %q", relative)
		}
		if entryInfo.Size() < 0 || entryInfo.Size() > limits.MaxFileBytes ||
			total > limits.MaxWorkspaceBytes-entryInfo.Size() {
			return violation("workspace_limit", "workspace exceeds its signed file envelope")
		}
		digest, readErr := hashFile(ctx, current, entryInfo.Size())
		if readErr != nil {
			return readErr
		}
		result[relative] = fileState{sha256: digest, size: entryInfo.Size(), mode: entryInfo.Mode() & fs.ModePerm, kind: "file"}
		total += entryInfo.Size()
		return nil
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

func treeSHA256(files map[string]fileState) (string, error) {
	paths := make([]string, 0, len(files))
	for filePath := range files {
		paths = append(paths, filePath)
	}
	sort.Strings(paths)
	entries := make([]treeEntry, 0, len(paths))
	for _, filePath := range paths {
		state := files[filePath]
		entries = append(entries, treeEntry{
			Path:      filePath,
			Type:      state.kind,
			Mode:      uint32(state.mode),
			SHA256:    state.sha256,
			SizeBytes: state.size,
		})
	}
	body, err := canonicalStruct(entries)
	if err != nil {
		return "", err
	}
	return sha256Hex(body), nil
}

func pathRoot(paths []string) (string, error) {
	body, err := canonicalStruct(paths)
	if err != nil {
		return "", err
	}
	return sha256Hex(body), nil
}

func atomicWrite(target string, body []byte, mode fs.FileMode) error {
	handle, err := os.CreateTemp(filepath.Dir(target), ".dittobench-write-*")
	if err != nil {
		return err
	}
	temporary := handle.Name()
	defer os.Remove(temporary)
	if err := handle.Chmod(mode); err != nil {
		handle.Close()
		return err
	}
	if _, err := handle.Write(body); err != nil {
		handle.Close()
		return err
	}
	if err := handle.Sync(); err != nil {
		handle.Close()
		return err
	}
	if err := handle.Close(); err != nil {
		return err
	}
	return os.Rename(temporary, target)
}
