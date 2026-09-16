//go:build linux

package probe

import (
	"net"
	"syscall"
	"time"
)

// tcpUserTimeout is TCP_USER_TIMEOUT (linux/tcp.h).
const tcpUserTimeout = 0x12

func setUserTimeout(conn *net.TCPConn, timeout time.Duration) error {
	raw, err := conn.SyscallConn()
	if err != nil {
		return err
	}
	var setErr error
	if err := raw.Control(func(fd uintptr) {
		setErr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_TCP, tcpUserTimeout, int(timeout/time.Millisecond))
	}); err != nil {
		return err
	}
	return setErr
}
