package rootlessnetns

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"

	"golang.org/x/sys/unix"
)

type wrappedListener struct{ net.Listener }

// plainTCPListener avoids Go's default MPTCP listener; the router listener is
// always IPPROTO_TCP.
func plainTCPListener(t *testing.T) net.Listener {
	t.Helper()
	var config net.ListenConfig
	config.SetMultipathTCP(false)
	listener, err := config.Listen(t.Context(), "tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return listener
}

func TestVerifyExistingListenerRequiresTopologyAddressAndNamespace(t *testing.T) {
	listener := plainTCPListener(t)
	defer listener.Close()
	address := listener.Addr().(*net.TCPAddr).AddrPort()
	config := Config{Address: address}
	child := func(net nsID) topologyPinner {
		return func(context.Context, Config) (*pinnedProcess, error) {
			return &pinnedProcess{facts: processFacts{net: net}}, nil
		}
	}
	current := nsID{dev: 4, ino: 42}
	sameNamespace := func(int) (nsID, error) { return current, nil }
	if err := verifyExistingListener(t.Context(), listener, config, child(current), sameNamespace); err != nil {
		t.Fatalf("baseline refused: %v", err)
	}
	for name, run := range map[string]func() error{
		"topology refused": func() error {
			return verifyExistingListener(t.Context(), listener, config, func(context.Context, Config) (*pinnedProcess, error) {
				return nil, ErrListener
			}, sameNamespace)
		},
		"nil child": func() error {
			return verifyExistingListener(t.Context(), listener, config, func(context.Context, Config) (*pinnedProcess, error) {
				return nil, nil
			}, sameNamespace)
		},
		// A daemon restart gives RootlessKit a new namespace; the listener still
		// lives in the old one.
		"restarted daemon namespace": func() error {
			return verifyExistingListener(t.Context(), listener, config, child(nsID{dev: 4, ino: 43}), sameNamespace)
		},
		"namespace unreadable": func() error {
			return verifyExistingListener(t.Context(), listener, config, child(current), func(int) (nsID, error) {
				return current, errors.New("denied")
			})
		},
		"other port": func() error {
			other := config
			other.Address = netip.AddrPortFrom(address.Addr(), address.Port()+1)
			return verifyExistingListener(t.Context(), listener, other, child(current), sameNamespace)
		},
		"other address": func() error {
			other := config
			other.Address = netip.AddrPortFrom(netip.MustParseAddr("127.0.0.2"), address.Port())
			return verifyExistingListener(t.Context(), listener, other, child(current), sameNamespace)
		},
		"wrapped listener without a socket": func() error {
			return verifyExistingListener(t.Context(), wrappedListener{listener}, config, child(current), sameNamespace)
		},
		"nil listener": func() error {
			return verifyExistingListener(t.Context(), nil, config, child(current), sameNamespace)
		},
		"no pinner": func() error {
			return verifyExistingListener(t.Context(), listener, config, nil, sameNamespace)
		},
		"multipath tcp listener": func() error {
			var mptcp net.ListenConfig
			mptcp.SetMultipathTCP(true)
			other, err := mptcp.Listen(t.Context(), "tcp4", "127.0.0.1:0")
			if err != nil {
				return errors.New("mptcp unavailable")
			}
			defer other.Close()
			otherConfig := Config{Address: other.Addr().(*net.TCPAddr).AddrPort()}
			if err := verifyExistingListener(t.Context(), other, otherConfig, child(current), sameNamespace); err != nil {
				return err
			}
			raw, err := other.(*net.TCPListener).SyscallConn()
			protocol := 0
			if err != nil || raw.Control(func(fd uintptr) {
				protocol, _ = unix.GetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_PROTOCOL)
			}) != nil || protocol != unix.IPPROTO_MPTCP {
				return errors.New("kernel fell back to TCP")
			}
			return nil
		},
		// SO_REUSEPORT would let another socket share the router port.
		"reuseport listener": func() error {
			shared := net.ListenConfig{Control: func(_, _ string, raw syscall.RawConn) error {
				var optionErr error
				if err := raw.Control(func(fd uintptr) {
					optionErr = unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_REUSEPORT, 1)
				}); err != nil {
					return err
				}
				return optionErr
			}}
			shared.SetMultipathTCP(false)
			other, err := shared.Listen(t.Context(), "tcp4", "127.0.0.1:0")
			if err != nil {
				// Linux always supports SO_REUSEPORT; never pass by accident.
				t.Fatalf("reuseport listener: %v", err)
			}
			defer other.Close()
			otherConfig := Config{Address: other.Addr().(*net.TCPAddr).AddrPort()}
			return verifyExistingListener(t.Context(), other, otherConfig, child(current), sameNamespace)
		},
		"no namespace reader": func() error {
			return verifyExistingListener(t.Context(), listener, config, child(current), nil)
		},
	} {
		if run() == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestVerifyExistingListenerRefusesNonListeningAndUnixSockets(t *testing.T) {
	current := nsID{dev: 4, ino: 42}
	pin := func(context.Context, Config) (*pinnedProcess, error) {
		return &pinnedProcess{facts: processFacts{net: current}}, nil
	}
	sameNamespace := func(int) (nsID, error) { return current, nil }
	unixListener, err := net.Listen("unix", filepath.Join(t.TempDir(), "control.sock"))
	if err != nil {
		t.Fatal(err)
	}
	defer unixListener.Close()
	if verifyExistingListener(t.Context(), unixListener, Config{Address: netip.MustParseAddrPort("127.0.0.1:18080")}, pin, sameNamespace) == nil {
		t.Fatal("unix listener accepted as the router listener")
	}
	closed := plainTCPListener(t)
	address := closed.Addr().(*net.TCPAddr).AddrPort()
	_ = closed.Close()
	if verifyExistingListener(t.Context(), closed, Config{Address: address}, pin, sameNamespace) == nil {
		t.Fatal("closed listener accepted")
	}
}

// Without RootlessKit the production path refuses at the topology step.
func TestVerifyListenerRefusesHostTopology(t *testing.T) {
	root, _ := stateFixture(t, strconv.Itoa(os.Getpid()), 0o444)
	socket := filepath.Join(root, "docker.sock")
	daemon, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer daemon.Close()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	sys := system{runRoot: root, procRoot: "/proc", nsenter: "/nonexistent/nsenter"}
	config := Config{Address: netip.MustParseAddrPort("172.17.0.1:18080"), DockerSocket: socket, HelperExecutable: "/approved/" + HelperExecutableName}
	pin := func(ctx context.Context, config Config) (*pinnedProcess, error) { return pinTopology(ctx, config, sys) }
	if verifyExistingListener(t.Context(), listener, config, pin, socketNetns) == nil {
		t.Fatal("host topology verified a listener")
	}
	if VerifyListener(t.Context(), listener, config) == nil {
		t.Fatal("production verification accepted a host listener")
	}
}
