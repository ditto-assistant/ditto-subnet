package codingcertservice

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcanary"
	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
)

type fakeControl struct {
	err   error
	calls int
}

func (control *fakeControl) Verify() error {
	control.calls++
	return control.err
}

type placementCalls struct{ docker, gateway, precheck, listener int }

func testPlacement(t *testing.T) (*Placement, *placementCalls, *fakeControl) {
	t.Helper()
	config, err := ConfigFromEnvironment(lookup(validEnvironment()), testUID)
	if err != nil {
		t.Fatal(err)
	}
	router, err := net.Listen("unix", privateTempDir(t)+"/router.sock")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = router.Close() })
	control := &fakeControl{}
	calls := &placementCalls{}
	placement := newPlacement(config, testUID, router, control)
	placement.verifyDockerSocket = func(path string, euid int) error {
		calls.docker++
		if path != DockerSocketPath || euid != testUID {
			return errors.New("wrong socket")
		}
		return nil
	}
	placement.bridgeGateway = func(_ context.Context, socket string) (netip.Addr, error) {
		calls.gateway++
		if socket != DockerSocketPath {
			return netip.Addr{}, errors.New("wrong socket")
		}
		return netip.MustParseAddr("10.203.0.1"), nil
	}
	placement.precheck = func(_ context.Context, config rootlessnetns.Config) error {
		calls.precheck++
		if config.DockerSocket != DockerSocketPath || config.HelperExecutable != RouterHelperPath ||
			config.Address != netip.MustParseAddrPort("10.203.0.1:11438") {
			return errors.New("wrong router config")
		}
		return nil
	}
	placement.verifyListener = func(_ context.Context, listener net.Listener, config rootlessnetns.Config) error {
		calls.listener++
		if listener != router || config.Address != netip.MustParseAddrPort("10.203.0.1:11438") {
			return errors.New("wrong listener")
		}
		return nil
	}
	return placement, calls, control
}

func TestPlacementReportsAllThreeProofsWhenPlaced(t *testing.T) {
	placement, calls, control := testPlacement(t)
	check := placement.Check(t.Context())
	if check != (codingcanary.TopologyCheck{RootlessTopology: true, ListenerNamespace: true, ControlSocket: true}) {
		t.Fatalf("check=%+v", check)
	}
	if calls.docker != 1 || calls.gateway != 1 || calls.precheck != 1 || calls.listener != 1 || control.calls != 1 {
		t.Fatalf("calls=%+v control=%d", calls, control.calls)
	}
}

// Every refusal path: a failed earlier proof reports nothing later as ready
// and does not run the later checks.
func TestPlacementRefusesEachUnprovenStepInOrder(t *testing.T) {
	cases := map[string]struct {
		mutate func(*Placement, *fakeControl)
		want   codingcanary.TopologyCheck
	}{
		"root process": {
			mutate: func(p *Placement, _ *fakeControl) { p.euid = 0 },
		},
		"another user": {
			// Every injected probe accepts, so only the service-user guard refuses.
			mutate: func(p *Placement, _ *fakeControl) {
				p.euid = testUID + 1
				p.verifyDockerSocket = func(string, int) error { return nil }
			},
		},
		"docker socket owner, mode or link": {
			mutate: func(p *Placement, _ *fakeControl) {
				p.verifyDockerSocket = func(string, int) error { return ErrSocket }
			},
		},
		"bridge gateway differs from the router address": {
			mutate: func(p *Placement, _ *fakeControl) {
				p.bridgeGateway = func(context.Context, string) (netip.Addr, error) {
					return netip.MustParseAddr("172.17.0.1"), nil
				}
			},
		},
		"bridge gateway unreadable": {
			mutate: func(p *Placement, _ *fakeControl) {
				p.bridgeGateway = func(context.Context, string) (netip.Addr, error) {
					return netip.Addr{}, errors.New("unavailable")
				}
			},
		},
		"rootful, detached or foreign daemon": {
			mutate: func(p *Placement, _ *fakeControl) {
				p.precheck = func(context.Context, rootlessnetns.Config) error { return rootlessnetns.ErrListener }
			},
		},
		"listener outside the current rootless namespace": {
			mutate: func(p *Placement, _ *fakeControl) {
				p.verifyListener = func(context.Context, net.Listener, rootlessnetns.Config) error {
					return rootlessnetns.ErrListener
				}
			},
			want: codingcanary.TopologyCheck{RootlessTopology: true},
		},
		"no router listener": {
			mutate: func(p *Placement, _ *fakeControl) { p.router = nil },
			want:   codingcanary.TopologyCheck{RootlessTopology: true},
		},
		"swapped control socket": {
			mutate: func(_ *Placement, control *fakeControl) { control.err = ErrSocket },
			want:   codingcanary.TopologyCheck{RootlessTopology: true, ListenerNamespace: true},
		},
		"no control socket": {
			mutate: func(p *Placement, _ *fakeControl) { p.control = nil },
			want:   codingcanary.TopologyCheck{RootlessTopology: true, ListenerNamespace: true},
		},
	}
	for name, test := range cases {
		t.Run(name, func(t *testing.T) {
			placement, calls, control := testPlacement(t)
			test.mutate(placement, control)
			if check := placement.Check(t.Context()); check != test.want {
				t.Fatalf("check=%+v want=%+v", check, test.want)
			}
			if !test.want.RootlessTopology && (calls.listener != 0 || control.calls != 0) {
				t.Fatalf("later checks ran after a topology refusal: %+v control=%d", calls, control.calls)
			}
		})
	}
	placement, _, _ := testPlacement(t)
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	if check := placement.Check(cancelled); check != (codingcanary.TopologyCheck{}) {
		t.Fatalf("cancelled check=%+v", check)
	}
	var missing *Placement
	if check := missing.Check(t.Context()); check != (codingcanary.TopologyCheck{}) {
		t.Fatalf("nil placement check=%+v", check)
	}
}
