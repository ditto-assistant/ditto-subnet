package codingcertservice

import (
	"context"
	"net"
	"net/netip"

	"github.com/ditto-assistant/dittobench-api/internal/codingcanary"
	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

// Placement proves, on demand, that the service is still placed as reviewed.
// Every check is read-only: no nsenter, no container, no listener creation.
type Placement struct {
	config  Config
	euid    int
	router  net.Listener
	control interface{ Verify() error }

	verifyDockerSocket func(path string, euid int) error
	bridgeGateway      func(ctx context.Context, socket string) (netip.Addr, error)
	precheck           func(ctx context.Context, config rootlessnetns.Config) error
	verifyListener     func(ctx context.Context, listener net.Listener, config rootlessnetns.Config) error
}

func newPlacement(config Config, euid int, router net.Listener, control interface{ Verify() error }) *Placement {
	return &Placement{
		config: config, euid: euid, router: router, control: control,
		verifyDockerSocket: VerifyDockerSocket,
		bridgeGateway:      sandbox.DefaultBridgeGatewayFromSocket,
		precheck:           rootlessnetns.Precheck,
		verifyListener:     rootlessnetns.VerifyListener,
	}
}

// RootlessTopology proves the dedicated daemon placement before any listener
// exists: the process runs as the configured non-root service user; the fixed
// Docker socket and its directory have that owner and the pinned modes; the
// daemon's default bridge gateway is the router address; and RootlessKit's
// child and the daemon behind that socket share the pinned, non-detached user
// and network namespaces owned by this user.
func (placement *Placement) RootlessTopology(ctx context.Context) bool {
	if placement == nil || ctx == nil || ctx.Err() != nil || placement.euid <= 0 ||
		placement.euid != placement.config.ServiceUID {
		return false
	}
	if placement.verifyDockerSocket(placement.config.DockerSocketPath, placement.euid) != nil {
		return false
	}
	gateway, err := placement.bridgeGateway(ctx, placement.config.DockerSocketPath)
	if err != nil || gateway != placement.config.RouterListen.Addr() {
		return false
	}
	return placement.precheck(ctx, placement.config.routerConfig()) == nil && ctx.Err() == nil
}

// Check is the readiness topology probe. Each flag requires the previous one.
func (placement *Placement) Check(ctx context.Context) codingcanary.TopologyCheck {
	var check codingcanary.TopologyCheck
	if !placement.RootlessTopology(ctx) {
		return check
	}
	check.RootlessTopology = true
	if placement.router == nil || placement.verifyListener(ctx, placement.router, placement.config.routerConfig()) != nil ||
		ctx.Err() != nil {
		return check
	}
	check.ListenerNamespace = true
	if placement.control == nil || placement.control.Verify() != nil {
		return check
	}
	check.ControlSocket = true
	return check
}
