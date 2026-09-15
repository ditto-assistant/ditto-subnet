package rootlessnetns

import (
	"context"
	"net"
	"net/netip"
	"syscall"
)

// VerifyListener re-proves, without starting nsenter or creating a socket,
// that listener is still the in-namespace listener Listen returned for config.
// It repeats every Precheck step (child pid, pinned child namespaces, the
// non-detached daemon behind the configured socket) and then requires the
// listener's own socket to be a listening IPv4 TCP socket bound exactly to
// config.Address, without SO_REUSEPORT, in the current RootlessKit child's
// network namespace.
//
// A long-lived service calls it on every readiness probe: when the rootless
// daemon restarts, RootlessKit creates a new network namespace, and a listener
// created in the old one no longer receives candidate traffic. That listener
// is refused rather than reported ready. VerifyListener never closes listener.
func VerifyListener(ctx context.Context, listener net.Listener, config Config) error {
	return verifyExistingListener(ctx, listener, config, func(ctx context.Context, config Config) (*pinnedProcess, error) {
		return pinTopology(ctx, config, productionSystem())
	}, socketNetns)
}

type topologyPinner func(context.Context, Config) (*pinnedProcess, error)

func verifyExistingListener(
	ctx context.Context,
	listener net.Listener,
	config Config,
	pin topologyPinner,
	netnsOf func(int) (nsID, error),
) error {
	if listener == nil || pin == nil || netnsOf == nil {
		return ErrListener
	}
	conn, ok := listener.(interface {
		SyscallConn() (syscall.RawConn, error)
	})
	if !ok {
		return ErrListener
	}
	child, err := pin(ctx, config)
	if err != nil || child == nil {
		return ErrListener
	}
	defer child.Close()
	return verifyRawListener(conn, config.Address, netnsOf, child.facts.net)
}

func verifyRawListener(
	conn interface {
		SyscallConn() (syscall.RawConn, error)
	},
	expected netip.AddrPort,
	netnsOf func(int) (nsID, error),
	want nsID,
) error {
	raw, err := conn.SyscallConn()
	if err != nil {
		return ErrListener
	}
	verifyErr := ErrListener
	if raw.Control(func(fd uintptr) {
		verifyErr = verifyListener(int(fd), expected, netnsOf, want)
	}) != nil || verifyErr != nil {
		return ErrListener
	}
	return nil
}
