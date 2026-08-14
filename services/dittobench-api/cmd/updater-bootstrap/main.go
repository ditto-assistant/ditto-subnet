// updater-bootstrap atomically refreshes a managed validator host's updater
// from the authenticated scorer image before dropping to the scorer uid.
package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"syscall"
)

const (
	bootstrapEnabled = "DITTOBENCH_BOOTSTRAP_VALIDATOR_STACK_UPDATER"
	bootstrapTarget  = "DITTOBENCH_BOOTSTRAP_VALIDATOR_STACK_UPDATER_TARGET_HOTKEY"
	validatorHotkey  = "DITTOBENCH_BOOTSTRAP_VALIDATOR_HOTKEY"
	scorerUID        = 65532
	scorerGID        = 65532
	sourceUpdater    = "/opt/ditto/validator-stack-auto-update.sh"
	hostScriptsDir   = "/opt/ditto/host-validator-scripts"
	updaterName      = "validator-stack-auto-update.sh"
)

func main() {
	bootstrap := false
	if os.Getenv(bootstrapEnabled) == "true" {
		var err error
		bootstrap, err = shouldBootstrapUpdater(
			os.Getenv(bootstrapTarget), os.Getenv(validatorHotkey),
		)
		if err != nil {
			log.Fatalf("select managed validator updater bootstrap: %v", err)
		}
		if os.Geteuid() != 0 {
			log.Fatalf("%s requires uid 0", bootstrapEnabled)
		}
		if bootstrap {
			if err := replaceUpdater(sourceUpdater, filepath.Join(hostScriptsDir, updaterName)); err != nil {
				log.Fatalf("refresh managed validator updater: %v", err)
			}
		}
	}
	if err := becomeScorer(); err != nil {
		log.Fatalf("drop scorer privileges: %v", err)
	}
	if os.Getenv(bootstrapEnabled) == "true" {
		if err := requireReadOnlyTree(hostScriptsDir); err != nil {
			log.Fatalf("isolate mounted host scripts from scorer: %v", err)
		}
	}
	if err := syscall.Exec("/dittobench-api", scorerArgs(os.Args[1:]), os.Environ()); err != nil {
		log.Fatalf("start scorer: %v", err)
	}
}

func shouldBootstrapUpdater(target, actual string) (bool, error) {
	if target == "" {
		return false, errors.New("signed bootstrap target hotkey is empty")
	}
	if actual == "" {
		return false, errors.New("host validator hotkey is empty")
	}
	return target == actual, nil
}

func scorerArgs(extra []string) []string {
	arguments := make([]string, 0, 3+len(extra))
	arguments = append(arguments, "/dittobench-api", "-port", "8000")
	return append(arguments, extra...)
}

func becomeScorer() error {
	switch os.Geteuid() {
	case scorerUID:
		return nil
	case 0:
		if err := syscall.Setgroups([]int{}); err != nil {
			return fmt.Errorf("clear supplementary groups: %w", err)
		}
		if err := syscall.Setgid(scorerGID); err != nil {
			return fmt.Errorf("set gid: %w", err)
		}
		if err := syscall.Setuid(scorerUID); err != nil {
			return fmt.Errorf("set uid: %w", err)
		}
		if os.Geteuid() != scorerUID || os.Getegid() != scorerGID {
			return errors.New("scorer uid/gid transition was not durable")
		}
		return nil
	default:
		return fmt.Errorf("unexpected bootstrap uid %d", os.Geteuid())
	}
}

func replaceUpdater(source, target string) error {
	sourceInfo, err := os.Lstat(source)
	if err != nil {
		return fmt.Errorf("inspect image source: %w", err)
	}
	if !sourceInfo.Mode().IsRegular() {
		return errors.New("image source is not a regular file")
	}
	targetInfo, err := os.Lstat(target)
	if err != nil {
		return fmt.Errorf("inspect host target: %w", err)
	}
	if !targetInfo.Mode().IsRegular() {
		return errors.New("host target is not a regular file")
	}
	parent := filepath.Dir(target)
	parentInfo, err := os.Lstat(parent)
	if err != nil {
		return fmt.Errorf("inspect host scripts directory: %w", err)
	}
	if !parentInfo.IsDir() || parentInfo.Mode()&os.ModeSymlink != 0 {
		return errors.New("host scripts path is not a real directory")
	}

	sourceBytes, err := os.ReadFile(source)
	if err != nil {
		return fmt.Errorf("read image updater: %w", err)
	}
	targetBytes, err := os.ReadFile(target)
	if err != nil {
		return fmt.Errorf("read host updater: %w", err)
	}
	if bytes.Equal(sourceBytes, targetBytes) && targetInfo.Mode().Perm() == 0o755 {
		return nil
	}

	temporary, err := os.CreateTemp(parent, ".validator-stack-auto-update.*.tmp")
	if err != nil {
		return fmt.Errorf("create updater replacement: %w", err)
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if _, err = io.Copy(temporary, bytes.NewReader(sourceBytes)); err != nil {
		temporary.Close()
		return fmt.Errorf("write updater replacement: %w", err)
	}
	if err = temporary.Chmod(0o755); err != nil {
		temporary.Close()
		return fmt.Errorf("make updater replacement executable: %w", err)
	}
	if stat, ok := targetInfo.Sys().(*syscall.Stat_t); ok {
		if err = temporary.Chown(int(stat.Uid), int(stat.Gid)); err != nil {
			temporary.Close()
			return fmt.Errorf("preserve updater ownership: %w", err)
		}
	}
	if err = temporary.Sync(); err != nil {
		temporary.Close()
		return fmt.Errorf("sync updater replacement: %w", err)
	}
	if err = temporary.Close(); err != nil {
		return fmt.Errorf("close updater replacement: %w", err)
	}
	if err = os.Rename(temporaryName, target); err != nil {
		return fmt.Errorf("install updater replacement: %w", err)
	}
	directory, err := os.Open(parent)
	if err != nil {
		return fmt.Errorf("open host scripts directory: %w", err)
	}
	defer directory.Close()
	if err = directory.Sync(); err != nil {
		return fmt.Errorf("sync host scripts directory: %w", err)
	}
	return nil
}

func requireReadOnlyTree(root string) error {
	return filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			// A private host checkout (for example, mode 0700) is stronger
			// isolation than an enumerable read-only tree. WalkDir reports the
			// denied directory a second time after the initial entry callback.
			// Accept it only when the scorer can neither mutate nor traverse the
			// directory. If traversal remains possible, fail closed because an
			// unlisted entry could still be reached by a known path.
			if errors.Is(walkErr, os.ErrPermission) {
				writeErr := syscall.Access(path, 2)
				if writeErr == nil {
					return fmt.Errorf("mounted host scripts remain writable at %q", path)
				}
				if !errors.Is(writeErr, syscall.EACCES) && !errors.Is(writeErr, syscall.EPERM) && !errors.Is(writeErr, syscall.EROFS) {
					return fmt.Errorf("check mounted host scripts permissions at %q: %w", path, writeErr)
				}
				executeErr := syscall.Access(path, 1)
				if errors.Is(executeErr, syscall.EACCES) || errors.Is(executeErr, syscall.EPERM) {
					return filepath.SkipDir
				}
			}
			return fmt.Errorf("inspect mounted host scripts: %w", walkErr)
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("mounted host scripts contain symlink %q", path)
		}
		if err := syscall.Access(path, 2); err == nil {
			return fmt.Errorf("mounted host scripts remain writable at %q", path)
		} else if !errors.Is(err, syscall.EACCES) && !errors.Is(err, syscall.EPERM) && !errors.Is(err, syscall.EROFS) {
			return fmt.Errorf("check mounted host scripts permissions at %q: %w", path, err)
		}
		return nil
	})
}
