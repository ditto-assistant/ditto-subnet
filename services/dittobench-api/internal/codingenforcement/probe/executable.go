package probe

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
)

// runningExecutable is the kernel's reference to the executable image this
// process was started from. Opening it reaches the inode that was mapped, even
// if the path it was started by has since been replaced or removed.
const runningExecutable = "/proc/self/exe"

// maxRunnerBytes bounds the measured binary.
const maxRunnerBytes = 256 << 20

// RunningExecutableSHA256 measures, on the host, the sha256 of the probe runner
// binary that is actually running. Evidence binds this digest to the
// release-recorded runtime.probe_runner_sha256; a source-tree hash is only
// supporting provenance.
func RunningExecutableSHA256() (string, error) {
	return fileSHA256(runningExecutable)
}

func fileSHA256(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > maxRunnerBytes {
		return "", errors.New("probe: running executable is not a bounded regular file")
	}
	hash := sha256.New()
	copied, err := io.Copy(hash, io.LimitReader(file, maxRunnerBytes+1))
	if err != nil {
		return "", err
	}
	if copied != info.Size() {
		return "", errors.New("probe: running executable changed while it was measured")
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
