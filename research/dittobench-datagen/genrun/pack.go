package genrun

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

// PackDir packages a generator directory exactly as it should be uploaded: the
// source files (challenge.CollectSource) plus a freshly vendored dependency
// tree when go.mod has requirements, as a deterministic archive that passes
// challenge.InspectArchive. Vendoring writes to a temporary directory, so the
// miner's checkout is never modified and a stale vendor/ is never shipped.
func PackDir(ctx context.Context, dir string) ([]byte, challenge.ArchiveInfo, error) {
	files, err := challenge.CollectSource(dir)
	if err != nil {
		return nil, challenge.ArchiveInfo{}, err
	}
	gomod, err := os.ReadFile(filepath.Join(dir, "go.mod"))
	if err != nil {
		return nil, challenge.ArchiveInfo{}, fmt.Errorf("generator directory needs go.mod: %w", err)
	}
	if hasRequire(gomod) {
		tmp, err := os.MkdirTemp("", "genrun-vendor-*")
		if err != nil {
			return nil, challenge.ArchiveInfo{}, err
		}
		defer os.RemoveAll(tmp)
		vendorDir := filepath.Join(tmp, "vendor")
		cmd := exec.CommandContext(ctx, "go", "mod", "vendor", "-o", vendorDir)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GOFLAGS=", "GOWORK=off")
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		if err := cmd.Run(); err != nil {
			return nil, challenge.ArchiveInfo{}, fmt.Errorf("go mod vendor: %v: %s", err, strings.TrimSpace(stderr.String()))
		}
		kept := files[:0]
		for _, f := range files {
			if f.Path != "vendor" && !strings.HasPrefix(f.Path, "vendor/") {
				kept = append(kept, f)
			}
		}
		vendored, err := challenge.CollectSource(vendorDir)
		if err != nil {
			return nil, challenge.ArchiveInfo{}, err
		}
		for _, f := range vendored {
			kept = append(kept, challenge.SourceFile{Path: "vendor/" + f.Path, Data: f.Data})
		}
		files = kept
	}
	raw, err := challenge.PackArchive(files)
	if err != nil {
		return nil, challenge.ArchiveInfo{}, err
	}
	info, _, err := challenge.InspectArchive(raw)
	if err != nil {
		return nil, challenge.ArchiveInfo{}, err
	}
	return raw, info, nil
}

func hasRequire(gomod []byte) bool {
	for _, line := range strings.Split(string(gomod), "\n") {
		line = strings.TrimSpace(line)
		if line == "require" || strings.HasPrefix(line, "require ") || strings.HasPrefix(line, "require(") {
			return true
		}
	}
	return false
}
