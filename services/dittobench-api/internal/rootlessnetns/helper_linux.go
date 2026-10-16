package rootlessnetns

import (
	"net/netip"

	"golang.org/x/sys/unix"
)

const (
	helperOK      = 0
	helperUsage   = 64
	helperFailure = 70
	listenBacklog = 128
)

// RunHelper is the entire one-shot helper. nsenter has already joined the
// pinned user and network namespaces. It accepts exactly `--listen <ipv4:port>`,
// requires fd 3 to be a SOCK_SEQPACKET Unix socket and fds 4 and 5 to be the
// inherited user and network namespace descriptors, creates one listening TCP
// socket without SO_REUSEPORT or IP_FREEBIND, sends it with the fixed protocol
// payload and exits. It prints nothing and reads no environment or files.
func RunHelper(args []string) int {
	if len(args) != 2 || args[0] != "--listen" {
		return helperUsage
	}
	address, err := netip.ParseAddrPort(args[1])
	if err != nil || address.String() != args[1] || !ValidAddress(address) {
		return helperUsage
	}
	if !controlSocket(helperSocketFD) {
		return helperFailure
	}
	// The worker passes exactly fds 3-5 and nsenter closes the namespace files
	// it opened itself, so 4 and 5 are the only inherited descriptors left to
	// drop. Close exactly those, after proving what they are. Never close a
	// range: by now the Go runtime owns descriptors too (for example the cgroup
	// cpu.max file it keeps open to update GOMAXPROCS), and closing one from
	// under it could let a later descriptor, such as the listener, reuse its
	// number.
	if !closeNamespace(helperUserFD, unix.CLONE_NEWUSER) || !closeNamespace(helperNetFD, unix.CLONE_NEWNET) {
		return helperFailure
	}
	listener, err := createListener(address)
	if err != nil {
		return helperFailure
	}
	defer unix.Close(listener)
	if unix.Sendmsg(helperSocketFD, []byte(Protocol), unix.UnixRights(listener), nil, 0) != nil {
		return helperFailure
	}
	return helperOK
}

// closeNamespace closes fd only if it is an inherited (not close-on-exec)
// namespace descriptor of the given CLONE_NEW* type.
func closeNamespace(fd int, kind int) bool {
	flags, err := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0)
	if err != nil || flags&unix.FD_CLOEXEC != 0 {
		return false
	}
	if _, err := nsIdentity(fd, kind); err != nil {
		return false
	}
	return unix.Close(fd) == nil
}

func controlSocket(fd int) bool {
	var stat unix.Stat_t
	if unix.Fstat(fd, &stat) != nil || stat.Mode&unix.S_IFMT != unix.S_IFSOCK {
		return false
	}
	domain, domainErr := unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_DOMAIN)
	kind, kindErr := unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_TYPE)
	return domainErr == nil && kindErr == nil && domain == unix.AF_UNIX && kind == unix.SOCK_SEQPACKET
}

// createListener binds without IP_FREEBIND, so the address must already be
// local in the joined network namespace.
func createListener(address netip.AddrPort) (int, error) {
	if !address.IsValid() || !address.Addr().Is4() {
		return -1, ErrListener
	}
	fd, err := unix.Socket(unix.AF_INET, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, unix.IPPROTO_TCP)
	if err != nil {
		return -1, ErrListener
	}
	local := &unix.SockaddrInet4{Port: int(address.Port()), Addr: address.Addr().As4()}
	// SO_REUSEADDR matches Go's own listeners: a restart after TIME_WAIT is
	// allowed while a second live listener on the same address is still refused.
	if unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_REUSEADDR, 1) != nil || unix.Bind(fd, local) != nil ||
		unix.Listen(fd, listenBacklog) != nil {
		_ = unix.Close(fd)
		return -1, ErrListener
	}
	name, err := unix.Getsockname(fd)
	bound, ok := name.(*unix.SockaddrInet4)
	if err != nil || !ok || bound.Addr != local.Addr || bound.Port != local.Port {
		_ = unix.Close(fd)
		return -1, ErrListener
	}
	return fd, nil
}
