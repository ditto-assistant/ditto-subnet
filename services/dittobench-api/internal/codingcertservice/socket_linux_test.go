package codingcertservice

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// privateTempDir is a short private test directory: its mode passes the
// ancestor check (t.TempDir follows the process umask) and its path keeps
// socket names under the 108-byte sun_path limit.
func privateTempDir(t *testing.T) string {
	t.Helper()
	directory, err := os.MkdirTemp("", "ccs")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(directory) })
	if err := os.Chmod(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	return directory
}

func controlDirectory(t *testing.T, mode os.FileMode) string {
	t.Helper()
	directory := filepath.Join(privateTempDir(t), "control")
	if err := os.Mkdir(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(directory, mode); err != nil {
		t.Fatal(err)
	}
	return directory
}

func listenTestControl(t *testing.T, directory string) *ControlSocket {
	t.Helper()
	control, err := ListenControlSocket(filepath.Join(directory, ControlSocketName), os.Geteuid(), os.Getegid())
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = control.Close() })
	return control
}

func TestControlSocketHasPinnedOwnerGroupModeAndServes(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("the control socket refuses root")
	}
	directory := controlDirectory(t, ControlDirectoryMode)
	control := listenTestControl(t, directory)
	info, err := os.Lstat(filepath.Join(directory, ControlSocketName))
	if err != nil || info.Mode()&os.ModeSocket == 0 || info.Mode().Perm() != ControlSocketMode {
		t.Fatalf("socket info=%v err=%v", info, err)
	}
	if err := control.Verify(); err != nil {
		t.Fatalf("verify: %v", err)
	}
	accepted := make(chan error, 1)
	go func() {
		conn, err := control.Listener().Accept()
		if err == nil {
			_ = conn.Close()
		}
		accepted <- err
	}()
	conn, err := net.Dial("unix", filepath.Join(directory, ControlSocketName))
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.Close()
	if err := <-accepted; err != nil {
		t.Fatal(err)
	}
	if control.Close() != nil {
		t.Fatal("close")
	}
	if _, err := os.Lstat(filepath.Join(directory, ControlSocketName)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("socket left behind: %v", err)
	}
	if control.Verify() == nil {
		t.Fatal("closed socket verified")
	}
}

func TestControlSocketRefusesUnsafeDirectoriesAndEntries(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("the control socket refuses root")
	}
	euid, egid := os.Geteuid(), os.Getegid()
	cases := map[string]func(t *testing.T) (path string, euid, gid int){
		"group-writable directory": func(t *testing.T) (string, int, int) {
			return filepath.Join(controlDirectory(t, 0o770), ControlSocketName), euid, egid
		},
		"world-readable directory": func(t *testing.T) (string, int, int) {
			return filepath.Join(controlDirectory(t, 0o755), ControlSocketName), euid, egid
		},
		"private directory without the control group": func(t *testing.T) (string, int, int) {
			return filepath.Join(controlDirectory(t, 0o700), ControlSocketName), euid, egid
		},
		"another control group": func(t *testing.T) (string, int, int) {
			return filepath.Join(controlDirectory(t, ControlDirectoryMode), ControlSocketName), euid, egid + 1
		},
		"another owner": func(t *testing.T) (string, int, int) {
			return filepath.Join(controlDirectory(t, ControlDirectoryMode), ControlSocketName), euid + 1, egid
		},
		"root owner": func(t *testing.T) (string, int, int) {
			return filepath.Join(controlDirectory(t, ControlDirectoryMode), ControlSocketName), 0, egid
		},
		"root group": func(t *testing.T) (string, int, int) {
			return filepath.Join(controlDirectory(t, ControlDirectoryMode), ControlSocketName), euid, 0
		},
		"symlinked directory": func(t *testing.T) (string, int, int) {
			real := controlDirectory(t, ControlDirectoryMode)
			link := filepath.Join(privateTempDir(t), "link")
			if err := os.Symlink(real, link); err != nil {
				t.Fatal(err)
			}
			return filepath.Join(link, ControlSocketName), euid, egid
		},
		"symlinked ancestor": func(t *testing.T) (string, int, int) {
			real := controlDirectory(t, ControlDirectoryMode)
			link := filepath.Join(privateTempDir(t), "ancestor")
			if err := os.Symlink(filepath.Dir(real), link); err != nil {
				t.Fatal(err)
			}
			return filepath.Join(link, filepath.Base(real), ControlSocketName), euid, egid
		},
		"group-writable ancestor": func(t *testing.T) (string, int, int) {
			real := controlDirectory(t, ControlDirectoryMode)
			if err := os.Chmod(filepath.Dir(real), 0o775); err != nil {
				t.Fatal(err)
			}
			return filepath.Join(real, ControlSocketName), euid, egid
		},
		"regular file squatting the path": func(t *testing.T) (string, int, int) {
			path := filepath.Join(controlDirectory(t, ControlDirectoryMode), ControlSocketName)
			if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
				t.Fatal(err)
			}
			return path, euid, egid
		},
		"symlink squatting the path": func(t *testing.T) (string, int, int) {
			path := filepath.Join(controlDirectory(t, ControlDirectoryMode), ControlSocketName)
			if err := os.Symlink(filepath.Join(privateTempDir(t), "elsewhere.sock"), path); err != nil {
				t.Fatal(err)
			}
			return path, euid, egid
		},
		"directory squatting the path": func(t *testing.T) (string, int, int) {
			path := filepath.Join(controlDirectory(t, ControlDirectoryMode), ControlSocketName)
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Fatal(err)
			}
			return path, euid, egid
		},
		"relative path": func(t *testing.T) (string, int, int) { return ControlSocketName, euid, egid },
		"unclean path": func(t *testing.T) (string, int, int) {
			directory := controlDirectory(t, ControlDirectoryMode)
			return directory + "/../" + filepath.Base(directory) + "/" + ControlSocketName, euid, egid
		},
	}
	for name, setup := range cases {
		t.Run(name, func(t *testing.T) {
			path, uid, gid := setup(t)
			before, beforeErr := os.Lstat(path)
			control, err := ListenControlSocket(path, uid, gid)
			if err == nil {
				_ = control.Close()
				t.Fatal("accepted")
			}
			if !errors.Is(err, ErrSocket) || err.Error() != ErrSocket.Error() {
				t.Fatalf("refusal carries detail: %v", err)
			}
			after, afterErr := os.Lstat(path)
			if (beforeErr == nil) != (afterErr == nil) || (beforeErr == nil && !os.SameFile(before, after)) {
				t.Fatal("refusal changed the squatting entry")
			}
		})
	}
}

func TestControlSocketReplacesOnlyItsOwnStaleSocket(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("the control socket refuses root")
	}
	directory := controlDirectory(t, ControlDirectoryMode)
	path := filepath.Join(directory, ControlSocketName)
	stale, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	stale.SetUnlinkOnClose(false)
	_ = stale.Close()
	control := listenTestControl(t, directory)
	if err := control.Verify(); err != nil {
		t.Fatal(err)
	}
}

// Every readiness probe re-proves the served socket. Each mutation below is a
// swap or relaxation an attacker or operator could make after startup.
func TestControlSocketVerifyRefusesPathSwapsAndRelaxations(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("the control socket refuses root")
	}
	mutations := map[string]func(t *testing.T, directory string){
		"socket mode relaxed": func(t *testing.T, directory string) {
			if err := os.Chmod(filepath.Join(directory, ControlSocketName), 0o666); err != nil {
				t.Fatal(err)
			}
		},
		"socket mode narrowed": func(t *testing.T, directory string) {
			if err := os.Chmod(filepath.Join(directory, ControlSocketName), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"directory mode relaxed": func(t *testing.T, directory string) {
			if err := os.Chmod(directory, 0o755); err != nil {
				t.Fatal(err)
			}
		},
		"socket replaced by another socket": func(t *testing.T, directory string) {
			path := filepath.Join(directory, ControlSocketName)
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			other, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = other.Close() })
			if err := os.Chmod(path, ControlSocketMode); err != nil {
				t.Fatal(err)
			}
		},
		"socket replaced by a symlink to itself": func(t *testing.T, directory string) {
			path := filepath.Join(directory, ControlSocketName)
			moved := filepath.Join(directory, "moved.sock")
			if err := os.Rename(path, moved); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(moved, path); err != nil {
				t.Fatal(err)
			}
		},
		"socket removed": func(t *testing.T, directory string) {
			if err := os.Remove(filepath.Join(directory, ControlSocketName)); err != nil {
				t.Fatal(err)
			}
		},
		"directory swapped for a copy": func(t *testing.T, directory string) {
			moved := directory + ".old"
			if err := os.Rename(directory, moved); err != nil {
				t.Fatal(err)
			}
			if err := os.Mkdir(directory, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(directory, ControlDirectoryMode); err != nil {
				t.Fatal(err)
			}
			if err := os.Rename(filepath.Join(moved, ControlSocketName), filepath.Join(directory, ControlSocketName)); err != nil {
				t.Fatal(err)
			}
		},
		"directory swapped for a symlink": func(t *testing.T, directory string) {
			moved := directory + ".old"
			if err := os.Rename(directory, moved); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(moved, directory); err != nil {
				t.Fatal(err)
			}
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			directory := controlDirectory(t, ControlDirectoryMode)
			control := listenTestControl(t, directory)
			if err := control.Verify(); err != nil {
				t.Fatalf("baseline: %v", err)
			}
			mutate(t, directory)
			if control.Verify() == nil {
				t.Fatal("mutated socket verified")
			}
		})
	}
}

func TestDockerSocketRequiresOwnerModesAndNoLinks(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("the service refuses root")
	}
	euid := os.Geteuid()
	daemon := func(t *testing.T, directoryMode, socketMode os.FileMode) string {
		t.Helper()
		directory := filepath.Join(privateTempDir(t), "docker")
		if err := os.Mkdir(directory, 0o700); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(directory, DockerSocketName)
		listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = listener.Close() })
		if err := os.Chmod(path, socketMode); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(directory, directoryMode); err != nil {
			t.Fatal(err)
		}
		return path
	}
	if err := VerifyDockerSocket(daemon(t, DockerDirectoryMode, DockerSocketMode), euid); err != nil {
		t.Fatalf("baseline: %v", err)
	}
	for name, path := range map[string]func(t *testing.T) string{
		"group socket":       func(t *testing.T) string { return daemon(t, DockerDirectoryMode, 0o660) },
		"traversable dir":    func(t *testing.T) string { return daemon(t, 0o750, DockerSocketMode) },
		"world-writable dir": func(t *testing.T) string { return daemon(t, 0o777, DockerSocketMode) },
		"symlinked socket": func(t *testing.T) string {
			real := daemon(t, DockerDirectoryMode, DockerSocketMode)
			link := filepath.Join(filepath.Dir(real), "link.sock")
			if err := os.Symlink(real, link); err != nil {
				t.Fatal(err)
			}
			return link
		},
		"symlinked directory": func(t *testing.T) string {
			real := daemon(t, DockerDirectoryMode, DockerSocketMode)
			link := filepath.Join(privateTempDir(t), "link")
			if err := os.Symlink(filepath.Dir(real), link); err != nil {
				t.Fatal(err)
			}
			return filepath.Join(link, DockerSocketName)
		},
		"regular file": func(t *testing.T) string {
			directory := filepath.Join(privateTempDir(t), "docker")
			if err := os.Mkdir(directory, DockerDirectoryMode); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(directory, DockerSocketName)
			if err := os.WriteFile(path, nil, DockerSocketMode); err != nil {
				t.Fatal(err)
			}
			return path
		},
		"missing": func(t *testing.T) string {
			return filepath.Join(privateTempDir(t), "docker", DockerSocketName)
		},
	} {
		t.Run(name, func(t *testing.T) {
			if VerifyDockerSocket(path(t), euid) == nil {
				t.Fatal("accepted")
			}
		})
	}
	if VerifyDockerSocket(daemon(t, DockerDirectoryMode, DockerSocketMode), euid+1) == nil {
		t.Fatal("another owner accepted")
	}
}

func TestTrustedExecutableMustBeRootOwnedUnwritableAndPinned(t *testing.T) {
	// /usr/bin/env is root-owned, not writable by others, below root-owned
	// directories on any supported host.
	if err := VerifyTrustedExecutable("/usr/bin/env", ""); err != nil {
		t.Fatalf("system executable refused: %v", err)
	}
	if VerifyTrustedExecutable("/usr/bin/env", strings.Repeat("0", 64)) == nil {
		t.Fatal("wrong pinned digest accepted")
	}
	body, err := os.ReadFile("/usr/bin/env")
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(body)
	if err := VerifyTrustedExecutable("/usr/bin/env", hex.EncodeToString(digest[:])); err != nil {
		t.Fatalf("pinned digest refused: %v", err)
	}
	directory := privateTempDir(t)
	own := filepath.Join(directory, "helper")
	if err := os.WriteFile(own, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(directory, "env-link")
	if err := os.Symlink("/usr/bin/env", link); err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{
		"user-owned helper":         own,
		"symlink to a system file":  link,
		"relative path":             "usr/bin/env",
		"unclean path":              "/usr/bin/../bin/env",
		"directory":                 "/usr/bin",
		"missing":                   "/usr/bin/ditto-certification-missing-helper",
		"below a user-owned folder": filepath.Join(directory, "missing"),
	} {
		if VerifyTrustedExecutable(path, "") == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestTrustedExecutableStatRequiresRootOwnedUnwritableExecutable(t *testing.T) {
	valid := unix.Stat_t{Mode: unix.S_IFREG | 0o755, Uid: 0, Size: 4096}
	if !trustedExecutableStat(valid) {
		t.Fatal("root-owned 0755 executable refused")
	}
	for name, mutate := range map[string]func(*unix.Stat_t){
		"service-user owned": func(stat *unix.Stat_t) { stat.Uid = 1000 },
		"group writable":     func(stat *unix.Stat_t) { stat.Mode = unix.S_IFREG | 0o775 },
		"world writable":     func(stat *unix.Stat_t) { stat.Mode = unix.S_IFREG | 0o757 },
		"not executable":     func(stat *unix.Stat_t) { stat.Mode = unix.S_IFREG | 0o644 },
		"not regular":        func(stat *unix.Stat_t) { stat.Mode = unix.S_IFDIR | 0o755 },
		"empty":              func(stat *unix.Stat_t) { stat.Size = 0 },
		"oversized":          func(stat *unix.Stat_t) { stat.Size = 256<<20 + 1 },
	} {
		stat := valid
		mutate(&stat)
		if trustedExecutableStat(stat) {
			t.Errorf("%s accepted", name)
		}
	}
}

// An ancestor owned by neither root nor the expected owner is refused even when
// it is not group or other writable: its owner could rename or replace it.
func TestSecureDirectoryRefusesAnAncestorOwnedByAnotherUser(t *testing.T) {
	euid := os.Geteuid()
	if euid == 0 {
		t.Skip("ownership of the test directory is meaningless for root")
	}
	child := filepath.Join(privateTempDir(t), "child")
	if err := os.Mkdir(child, 0o700); err != nil {
		t.Fatal(err)
	}
	accept := func(unix.Stat_t) bool { return true }
	fd, _, err := openSecureDirectory(child, euid, accept)
	if err != nil {
		t.Fatalf("own ancestors refused: %v", err)
	}
	_ = unix.Close(fd)
	// With root as the only acceptable owner, the test user's directory above
	// child is a foreign ancestor.
	if fd, _, err := openSecureDirectory(child, 0, accept); err == nil {
		_ = unix.Close(fd)
		t.Fatal("ancestor owned by another user accepted")
	}
}
