//go:build unix

package codingartifactcanary

import (
	"os"
	"syscall"
)

func fileOwner(info os.FileInfo) (uint32, bool) {
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, false
	}
	return metadata.Uid, true
}
