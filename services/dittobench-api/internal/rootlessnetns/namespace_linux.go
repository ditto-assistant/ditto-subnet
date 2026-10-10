package rootlessnetns

import (
	"bufio"
	"bytes"
	"context"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

const (
	// dockerd-rootless.sh uses $XDG_RUNTIME_DIR/dockerd-rootless unless
	// DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR overrides it. The native daemon
	// unit sets XDG_RUNTIME_DIR=/run/user/<uid> and no override. RootlessKit
	// writes the decimal child pid, without a newline, after network setup.
	runUserRoot   = "/run/user"
	stateDirName  = "dockerd-rootless"
	childPIDName  = "child_pid"
	maxChildPID   = 4194304 // PID_MAX_LIMIT on 64-bit Linux
	maxStatusSize = 16 << 10
)

// nsID is a namespace inode identity. Namespace files share the nsfs device.
type nsID struct{ dev, ino uint64 }

// processFacts are observed kernel facts about one live process. They are
// collected before any decision so the decision itself is a pure function.
type processFacts struct {
	uids       [4]uint32 // real, effective, saved and filesystem UIDs as seen here
	user       nsID
	userOwner  uint32
	userParent nsID
	net        nsID
	netOwner   nsID
}

type selfFacts struct{ user, net nsID }

// validTopology accepts only RootlessKit's non-detached layout: the child and
// the daemon behind the configured socket share one user namespace directly
// below this process's user namespace and owned by this UID, and one network
// namespace owned by that user namespace and distinct from this process's.
// Detached-netns RootlessKit (the default in newer dockerd-rootless.sh) runs the
// daemon in the host network namespace while the child holds the detached one,
// so the daemon/child equality refuses it rather than guessing.
func validTopology(euid uint32, self selfFacts, child, daemon processFacts, peerUID uint32) bool {
	zero := nsID{}
	return self.user != zero && self.net != zero && child.user != zero && child.net != zero &&
		child.user != self.user && child.net != self.net &&
		child.userOwner == euid && child.userParent == self.user && child.netOwner == child.user &&
		sameUIDs(child.uids, euid) &&
		daemon.user == child.user && daemon.net == child.net && daemon.netOwner == child.user &&
		sameUIDs(daemon.uids, euid) && peerUID == euid
}

func sameUIDs(uids [4]uint32, euid uint32) bool {
	return uids[0] == euid && uids[1] == euid && uids[2] == euid && uids[3] == euid
}

// readChildPID reads RootlessKit's child pid from the fixed state path for
// euid without following links. The runtime directory must be private to the
// UID and the state directory must not be writable by other users. Inside the
// worker unit, systemd's ProtectHome=tmpfs plus a single-file bind synthesizes
// both directories as root-owned 0755, so root-owned directories that only
// root can modify are also accepted. The file itself must always be the UID's
// read-only single-link file RootlessKit writes.
func readChildPID(runRoot string, euid int) (int, error) {
	if euid < 0 || !cleanAbsolute(runRoot) {
		return 0, ErrListener
	}
	root, err := unix.Open(runRoot, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return 0, ErrListener
	}
	defer unix.Close(root)
	// /run/user is root-owned on systemd hosts. As for other private inputs, a
	// root-owned sticky parent is accepted: other users cannot replace the
	// UID-owned entry below it, and a squatted entry fails its owner check.
	if !fdMatches(root, unix.S_IFDIR, func(uid uint32, perm uint32) bool {
		return (uid == 0 || uid == uint32(euid)) && (perm&0o022 == 0 || uid == 0 && perm&unix.S_ISVTX != 0)
	}) {
		return 0, ErrListener
	}
	runtime, err := unix.Openat(root, strconv.Itoa(euid), unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return 0, ErrListener
	}
	defer unix.Close(runtime)
	if !fdMatches(runtime, unix.S_IFDIR, func(uid uint32, perm uint32) bool {
		return uid == uint32(euid) && perm&0o077 == 0 || uid == 0 && perm&0o022 == 0
	}) {
		return 0, ErrListener
	}
	state, err := unix.Openat(runtime, stateDirName, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return 0, ErrListener
	}
	defer unix.Close(state)
	if !fdMatches(state, unix.S_IFDIR, func(uid uint32, perm uint32) bool { return (uid == uint32(euid) || uid == 0) && perm&0o022 == 0 }) {
		return 0, ErrListener
	}
	fd, err := unix.Openat(state, childPIDName, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return 0, ErrListener
	}
	file := os.NewFile(uintptr(fd), "rootlesskit-child-pid")
	defer file.Close()
	var stat unix.Stat_t
	if unix.Fstat(fd, &stat) != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Uid != uint32(euid) ||
		stat.Mode&0o333 != 0 || stat.Nlink != 1 || stat.Size < 1 || stat.Size > int64(len(strconv.Itoa(maxChildPID))) {
		return 0, ErrListener
	}
	body, err := io.ReadAll(io.LimitReader(file, stat.Size+1))
	if err != nil || int64(len(body)) != stat.Size {
		return 0, ErrListener
	}
	return parseChildPID(body)
}

func parseChildPID(body []byte) (int, error) {
	if len(body) == 0 || body[0] == '0' || bytes.ContainsFunc(body, func(r rune) bool { return r < '0' || r > '9' }) {
		return 0, ErrListener
	}
	pid, err := strconv.Atoi(string(body))
	if err != nil || pid < 2 || pid > maxChildPID {
		return 0, ErrListener
	}
	return pid, nil
}

func fdMatches(fd int, kind uint32, allowed func(uid uint32, perm uint32) bool) bool {
	var stat unix.Stat_t
	return unix.Fstat(fd, &stat) == nil && stat.Mode&unix.S_IFMT == kind && allowed(stat.Uid, stat.Mode&0o7777)
}

// nsIdentity requires an nsfs descriptor of the given CLONE_NEW* type.
func nsIdentity(fd int, kind int) (nsID, error) {
	var fs unix.Statfs_t
	var stat unix.Stat_t
	if unix.Fstatfs(fd, &fs) != nil || fs.Type != unix.NSFS_MAGIC || unix.Fstat(fd, &stat) != nil {
		return nsID{}, ErrListener
	}
	if got, err := unix.IoctlRetInt(fd, unix.NS_GET_NSTYPE); err != nil || got != kind {
		return nsID{}, ErrListener
	}
	return nsID{dev: stat.Dev, ino: stat.Ino}, nil
}

// relatedIdentity resolves an nsfs ioctl that returns a namespace descriptor.
func relatedIdentity(fd int, request uint, kind int) (nsID, error) {
	related, err := unix.IoctlRetInt(fd, request)
	if err != nil {
		return nsID{}, ErrListener
	}
	defer unix.Close(related)
	return nsIdentity(related, kind)
}

func openNamespace(procRoot, process, name string, kind int) (*os.File, nsID, error) {
	fd, err := unix.Open(filepath.Join(procRoot, process, "ns", name), unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, nsID{}, ErrListener
	}
	file := os.NewFile(uintptr(fd), "namespace")
	id, err := nsIdentity(fd, kind)
	if err != nil {
		_ = file.Close()
		return nil, nsID{}, ErrListener
	}
	return file, id, nil
}

func inspectSelf(procRoot string) (selfFacts, error) {
	user, userID, err := openNamespace(procRoot, "self", "user", unix.CLONE_NEWUSER)
	if err != nil {
		return selfFacts{}, ErrListener
	}
	_ = user.Close()
	netns, netID, err := openNamespace(procRoot, "self", "net", unix.CLONE_NEWNET)
	if err != nil {
		return selfFacts{}, ErrListener
	}
	_ = netns.Close()
	return selfFacts{user: userID, net: netID}, nil
}

// pinnedProcess retains the exact user and network namespace descriptors that
// were inspected. nsenter joins these descriptors, never a pid path that could
// be reused after validation.
type pinnedProcess struct {
	facts processFacts
	user  *os.File
	net   *os.File
}

func (p *pinnedProcess) Close() {
	if p == nil {
		return
	}
	if p.user != nil {
		_ = p.user.Close()
	}
	if p.net != nil {
		_ = p.net.Close()
	}
}

// inspectProcess opens a pidfd first and proves the process is still alive
// after its namespace and status files were read, so a pid reused during
// inspection cannot supply another process's facts.
func inspectProcess(procRoot string, pid int) (*pinnedProcess, error) {
	if pid < 2 || pid > maxChildPID {
		return nil, ErrListener
	}
	pidfd, err := unix.PidfdOpen(pid, 0)
	if err != nil {
		return nil, ErrListener
	}
	defer unix.Close(pidfd)
	process := strconv.Itoa(pid)
	pinned := &pinnedProcess{}
	fail := func() (*pinnedProcess, error) {
		pinned.Close()
		return nil, ErrListener
	}
	if pinned.user, pinned.facts.user, err = openNamespace(procRoot, process, "user", unix.CLONE_NEWUSER); err != nil {
		return fail()
	}
	owner, err := unix.IoctlGetUint32(int(pinned.user.Fd()), unix.NS_GET_OWNER_UID)
	if err != nil {
		return fail()
	}
	pinned.facts.userOwner = owner
	// NS_GET_PARENT refuses a namespace outside this process's scope, which
	// includes the initial user namespace itself.
	if pinned.facts.userParent, err = relatedIdentity(int(pinned.user.Fd()), unix.NS_GET_PARENT, unix.CLONE_NEWUSER); err != nil {
		return fail()
	}
	if pinned.net, pinned.facts.net, err = openNamespace(procRoot, process, "net", unix.CLONE_NEWNET); err != nil {
		return fail()
	}
	if pinned.facts.netOwner, err = relatedIdentity(int(pinned.net.Fd()), unix.NS_GET_USERNS, unix.CLONE_NEWUSER); err != nil {
		return fail()
	}
	if pinned.facts.uids, err = readStatusUIDs(filepath.Join(procRoot, process, "status")); err != nil {
		return fail()
	}
	if unix.PidfdSendSignal(pidfd, 0, nil, 0) != nil {
		return fail()
	}
	return pinned, nil
}

func readStatusUIDs(path string) ([4]uint32, error) {
	var uids [4]uint32
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return uids, ErrListener
	}
	file := os.NewFile(uintptr(fd), "process-status")
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, maxStatusSize+1))
	if err != nil || len(body) > maxStatusSize {
		return uids, ErrListener
	}
	return parseStatusUIDs(body)
}

func parseStatusUIDs(body []byte) ([4]uint32, error) {
	var uids [4]uint32
	found := false
	scanner := bufio.NewScanner(bytes.NewReader(body))
	for scanner.Scan() {
		value, ok := strings.CutPrefix(scanner.Text(), "Uid:")
		if !ok {
			continue
		}
		fields := strings.Fields(value)
		if found || len(fields) != 4 {
			return uids, ErrListener
		}
		for i, field := range fields {
			parsed, err := strconv.ParseUint(field, 10, 32)
			if err != nil || strconv.FormatUint(parsed, 10) != field {
				return uids, ErrListener
			}
			uids[i] = uint32(parsed)
		}
		found = true
	}
	if scanner.Err() != nil || !found {
		return uids, ErrListener
	}
	return uids, nil
}

// daemonPeer returns the credentials of the process that listens on the
// configured Docker socket. The kernel records them at listen time.
func daemonPeer(ctx context.Context, socket string) (int, uint32, error) {
	if !cleanAbsolute(socket) {
		return 0, 0, ErrListener
	}
	dialer := net.Dialer{Timeout: 5 * time.Second}
	conn, err := dialer.DialContext(ctx, "unix", socket)
	if err != nil {
		return 0, 0, ErrListener
	}
	defer conn.Close()
	raw, err := conn.(*net.UnixConn).SyscallConn()
	if err != nil {
		return 0, 0, ErrListener
	}
	var credentials *unix.Ucred
	var optionErr error
	if raw.Control(func(fd uintptr) {
		credentials, optionErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	}) != nil || optionErr != nil || credentials == nil || credentials.Pid < 2 {
		return 0, 0, ErrListener
	}
	return int(credentials.Pid), credentials.Uid, nil
}
