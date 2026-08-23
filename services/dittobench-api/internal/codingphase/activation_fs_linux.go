//go:build linux

package codingphase

import (
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

func ensureJournalDirectory(parentPath, leaf string) error {
	if !validJournalLeaf(leaf) {
		return errors.New("coding relay journal leaf is invalid")
	}
	parentFD, err := unix.Open(
		parentPath,
		unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC,
		0,
	)
	if err != nil {
		return fmt.Errorf("open coding relay journal parent: %w", err)
	}
	defer unix.Close(parentFD)
	var parentStat unix.Stat_t
	if err := unix.Fstat(parentFD, &parentStat); err != nil ||
		parentStat.Mode&unix.S_IFMT != unix.S_IFDIR || parentStat.Mode&0o777 != 0o700 ||
		parentStat.Uid != uint32(os.Geteuid()) {
		return fmt.Errorf(
			"coding relay journal parent ownership or permissions are unsafe: mode=%#o uid=%d euid=%d",
			parentStat.Mode, parentStat.Uid, os.Geteuid(),
		)
	}
	if err := unix.Mkdirat(parentFD, leaf, 0o700); err != nil && !errors.Is(err, unix.EEXIST) {
		return fmt.Errorf("create coding relay journal root: %w", err)
	}
	var childStat unix.Stat_t
	if err := unix.Fstatat(parentFD, leaf, &childStat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		childStat.Mode&unix.S_IFMT != unix.S_IFDIR || childStat.Mode&0o777 != 0o700 ||
		childStat.Uid != uint32(os.Geteuid()) || childStat.Dev != parentStat.Dev {
		return errors.New("coding relay journal root ownership or permissions are unsafe")
	}
	// Always repeat the parent sync. A previous call may have installed the
	// directory and then lost the fsync response, making EEXIST an ambiguous
	// durability result rather than proof that the name is stable.
	if err := unix.Fsync(parentFD); err != nil {
		return fmt.Errorf("sync coding relay journal parent: %w", err)
	}
	return nil
}

func validJournalLeaf(value string) bool {
	if len(value) != len("relay-")+64 || value[:len("relay-")] != "relay-" {
		return false
	}
	for _, character := range value[len("relay-"):] {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}
