package codinghostedruntime

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
)

func rootlessHelper(t *testing.T) string {
	t.Helper()
	worker, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Join(filepath.Dir(worker), rootlessnetns.HelperExecutableName)
}

// routerAuthority is a valid connectivity expiry before the fixture's
// twenty-minute attempt deadline.
func routerAuthority() int64 { return time.Now().Add(10 * time.Minute).Unix() }

func rootlessExecutable(helper string) func(string) bool {
	return func(path string) bool {
		return testExecutable(path) || path == helper || path == rootlessnetns.NsenterExecutable
	}
}

func TestRouterNamespaceDefaultsToExistingHostListener(t *testing.T) {
	for _, mode := range []string{"", routerNamespaceHost} {
		wire, path := fixture(t)
		wire.RouterNamespace = mode
		writeConfig(t, path, wire)
		// Even with the rootless prerequisites installed, the default stays host.
		config, err := loadConfigChecked(path, rootlessExecutable(rootlessHelper(t)))
		if err != nil {
			t.Fatalf("mode %q rejected: %v", mode, err)
		}
		if config.router != nil || config.docker.HostGatewayIP != "172.21.0.1" || config.publicBase != "http://host.docker.internal:19010" {
			t.Fatalf("mode %q did not keep the host listener", mode)
		}
	}
	// An omitted field is not only accepted, it is the current wire shape.
	wire, path := fixture(t)
	writeConfig(t, path, wire)
	body, err := os.ReadFile(path)
	if err != nil || !strings.Contains(string(body), `"router_namespace":""`) {
		t.Fatal("fixture shape")
	}
	write(t, path, []byte(strings.Replace(string(body), `"router_namespace":"",`, "", 1)))
	if config, err := loadConfigChecked(path, testExecutable); err != nil || config.router != nil {
		t.Fatal("omitted router namespace changed behavior")
	}
}

func TestRootlessRouterNamespaceBindsGatewayHelperAndNsenter(t *testing.T) {
	helper := rootlessHelper(t)
	wire, path := fixture(t)
	wire.RouterNamespace = routerNamespaceRootless
	wire.RouterListen = "172.17.0.1:18080"
	wire.RouterExpiresAtUnix = routerAuthority()
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, rootlessExecutable(helper))
	if err != nil {
		t.Fatalf("rootless router namespace rejected: %v", err)
	}
	if config.router == nil || config.router.address != netip.MustParseAddrPort("172.17.0.1:18080") || config.router.helper != helper {
		t.Fatal("rootless router binding drift")
	}
	// Candidates resolve host.docker.internal to the in-namespace gateway, never
	// to discovered eth0 or Docker's host-gateway placeholder.
	if !config.docker.RequireRootless || config.docker.HostGatewayIP != "172.17.0.1" || config.publicBase != "http://host.docker.internal:18080" {
		t.Fatal("candidate gateway is not the in-namespace router address")
	}
	if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
		t.Fatal("validation consumed attempt")
	}
}

func TestRootlessRouterNamespaceRejectsDriftBeforeConsumingAttempt(t *testing.T) {
	helper := rootlessHelper(t)
	for name, tc := range map[string]struct {
		mode, listen string
		executable   func(string) bool
	}{
		"unknown_mode":       {mode: "rootless", listen: "172.17.0.1:18080", executable: rootlessExecutable(helper)},
		"case_mode":          {mode: "Rootless-Netns", listen: "172.17.0.1:18080", executable: rootlessExecutable(helper)},
		"padded_mode":        {mode: " rootless-netns", listen: "172.17.0.1:18080", executable: rootlessExecutable(helper)},
		"slirp_mode":         {mode: "slirp4netns", listen: "172.17.0.1:18080", executable: rootlessExecutable(helper)},
		"unprotected_helper": {mode: routerNamespaceRootless, listen: "172.17.0.1:18080", executable: rootlessExecutable("")},
		"unprotected_nsenter": {mode: routerNamespaceRootless, listen: "172.17.0.1:18080", executable: func(path string) bool {
			return testExecutable(path) || path == helper
		}},
		"loopback":  {mode: routerNamespaceRootless, listen: "127.0.0.1:18080", executable: rootlessExecutable(helper)},
		"wildcard":  {mode: routerNamespaceRootless, listen: "0.0.0.0:18080", executable: rootlessExecutable(helper)},
		"public":    {mode: routerNamespaceRootless, listen: "8.8.8.8:18080", executable: rootlessExecutable(helper)},
		"low_port":  {mode: routerNamespaceRootless, listen: "172.17.0.1:80", executable: rootlessExecutable(helper)},
		"ipv6":      {mode: routerNamespaceRootless, listen: "[fd00::1]:18080", executable: rootlessExecutable(helper)},
		"noncanon":  {mode: routerNamespaceRootless, listen: "172.17.0.1:018080", executable: rootlessExecutable(helper)},
		"no_port":   {mode: routerNamespaceRootless, listen: "172.17.0.1", executable: rootlessExecutable(helper)},
		"hostname":  {mode: routerNamespaceRootless, listen: "gateway:18080", executable: rootlessExecutable(helper)},
		"four_in_6": {mode: routerNamespaceRootless, listen: "[::ffff:172.17.0.1]:18080", executable: rootlessExecutable(helper)},
	} {
		t.Run(name, func(t *testing.T) {
			wire, path := fixture(t)
			wire.RouterNamespace, wire.RouterListen, wire.RouterExpiresAtUnix = tc.mode, tc.listen, routerAuthority()
			writeConfig(t, path, wire)
			if _, err := loadConfigChecked(path, tc.executable); err != ErrConfig {
				t.Fatal("rootless router drift accepted")
			}
			if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
				t.Fatal("invalid config consumed attempt")
			}
		})
	}
}

// router_listen is parsed once with the shared rootless address policy in both
// namespaces. Host mode formerly also accepted two non-canonical spellings of an
// IPv4 address (a bracketed IPv4 literal and an IPv4-mapped IPv6 literal); both
// are now refused in either mode. Canonical private IPv4 input is unchanged.
func TestRouterListenUsesOneAddressPolicyInBothNamespaces(t *testing.T) {
	helper := rootlessHelper(t)
	for _, mode := range []string{routerNamespaceHost, routerNamespaceRootless} {
		for listen, accepted := range map[string]bool{
			"172.21.0.1:19010":          true,
			"10.33.0.2:18080":           true,
			"192.168.1.20:65535":        true,
			"172.21.0.1:1024":           true,
			"172.21.0.1:1023":           false,
			"172.21.0.1:019010":         false,
			"172.21.0.1:+19010":         false,
			"172.021.0.1:19010":         false,
			"[172.21.0.1]:19010":        false,
			"[::ffff:172.21.0.1]:19010": false,
			"[fd00::1]:19010":           false,
			"0.0.0.0:19010":             false,
			"127.0.0.1:19010":           false,
			"8.8.8.8:19010":             false,
			"169.254.1.1:19010":         false,
			"172.21.0.1":                false,
			" 172.21.0.1:19010":         false,
			"localhost:19010":           false,
		} {
			wire, path := fixture(t)
			wire.RouterNamespace, wire.RouterListen = mode, listen
			if mode == routerNamespaceRootless {
				wire.RouterExpiresAtUnix = routerAuthority()
			}
			writeConfig(t, path, wire)
			config, err := loadConfigChecked(path, rootlessExecutable(helper))
			if (err == nil) != accepted {
				t.Fatalf("mode %s listen %q: accepted=%v err=%v", mode, listen, err == nil, err)
			}
			if err != nil {
				continue
			}
			address := netip.MustParseAddrPort(listen)
			if config.docker.HostGatewayIP != address.Addr().String() ||
				config.publicBase != "http://host.docker.internal:"+strconv.Itoa(int(address.Port())) {
				t.Fatalf("mode %s listen %q: derived gateway or public URL drift", mode, listen)
			}
		}
	}
}

func fakeDocker(t *testing.T, gateway string) {
	t.Helper()
	dir := privateTemp(t)
	script := "#!/bin/sh\n[ \"$1 $2 $5\" = \"network inspect bridge\" ] || exit 9\n" +
		"printf '%s\\n' '{\"Name\":\"bridge\",\"Driver\":\"bridge\",\"Internal\":false,\"IPAM\":{\"Config\":[{\"Subnet\":\"" +
		netip.PrefixFrom(netip.MustParseAddr(gateway), 16).Masked().String() + "\",\"Gateway\":\"" + gateway + "\"}]},\"Options\":{\"com.docker.network.bridge.default_bridge\":\"true\"}}'\n"
	if os.WriteFile(filepath.Join(dir, "docker"), []byte(script), 0o700) != nil {
		t.Fatal("docker fixture")
	}
	t.Setenv("PATH", dir)
}

// The rootless mode must never fall back to a host-namespace listener, even
// when the configured gateway happens to be a local host address.
func TestRootlessRouterNeverFallsBackToHostListener(t *testing.T) {
	var local netip.Addr
	addresses, err := net.InterfaceAddrs()
	if err != nil {
		t.Fatal(err)
	}
	for _, address := range addresses {
		prefix, err := netip.ParsePrefix(address.String())
		if err == nil && prefix.Addr().Is4() && prefix.Addr().IsPrivate() && !prefix.Addr().IsLoopback() {
			local = prefix.Addr()
			break
		}
	}
	if !local.IsValid() {
		t.Skip("no local private IPv4 address")
	}
	probe, err := net.Listen("tcp4", netip.AddrPortFrom(local, 0).String())
	if err != nil {
		t.Fatal(err)
	}
	address := netip.MustParseAddrPort(probe.Addr().String())
	_ = probe.Close()
	helper := rootlessHelper(t)
	wire, path := fixture(t)
	wire.RouterNamespace, wire.RouterListen = routerNamespaceRootless, address.String()
	wire.RouterExpiresAtUnix = routerAuthority()
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, rootlessExecutable(helper))
	if err != nil {
		t.Fatalf("fixture rejected: %v", err)
	}
	previous := rootlessListen
	t.Cleanup(func() { rootlessListen = previous })
	for name, gateway := range map[string]string{"gateway_mismatch": "172.31.255.254", "no_rootlesskit": local.String()} {
		t.Run(name, func(t *testing.T) {
			fakeDocker(t, gateway)
			called := false
			// Delegate to the real RootlessKit listener; only record the call.
			rootlessListen = func(ctx context.Context, got rootlessnetns.Config) (net.Listener, error) {
				called = true
				return rootlessnetns.Listen(ctx, got)
			}
			if listener, err := listenRouter(t.Context(), config); err == nil {
				_ = listener.Close()
				t.Fatal("rootless mode produced a listener without RootlessKit")
			}
			if called != (name == "no_rootlesskit") {
				t.Fatalf("real namespace listener called=%v", called)
			}
			free, err := net.Listen("tcp4", address.String())
			if err != nil {
				t.Fatal("rootless mode bound the configured address in the host namespace")
			}
			_ = free.Close()
		})
	}
	t.Run("docker_unavailable", func(t *testing.T) {
		t.Setenv("PATH", privateTemp(t))
		if listener, err := listenRouter(t.Context(), config); err == nil {
			_ = listener.Close()
			t.Fatal("rootless mode listened without the daemon bridge")
		}
	})
}

func TestRootlessRouterRequiresDaemonGatewayBeforeNamespaceListener(t *testing.T) {
	helper := rootlessHelper(t)
	wire, path := fixture(t)
	wire.RouterNamespace, wire.RouterListen = routerNamespaceRootless, "172.17.0.1:18080"
	wire.RouterExpiresAtUnix = routerAuthority()
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, rootlessExecutable(helper))
	if err != nil {
		t.Fatalf("fixture rejected: %v", err)
	}
	var calls []rootlessnetns.Config
	sentinel, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer sentinel.Close()
	previous := rootlessListen
	t.Cleanup(func() { rootlessListen = previous })
	rootlessListen = func(_ context.Context, got rootlessnetns.Config) (net.Listener, error) {
		calls = append(calls, got)
		return sentinel, nil
	}
	for _, gateway := range []string{"172.17.0.2", "172.18.0.1"} {
		fakeDocker(t, gateway)
		if _, err := listenRouter(t.Context(), config); err == nil {
			t.Fatalf("gateway %s accepted for router 172.17.0.1", gateway)
		}
	}
	if len(calls) != 0 {
		t.Fatal("namespace listener requested before the daemon gateway matched")
	}
	fakeDocker(t, "172.17.0.1")
	listener, err := listenRouter(t.Context(), config)
	if err != nil || listener == sentinel || listener.Addr() != sentinel.Addr() {
		t.Fatalf("matching gateway refused or served without its authority bound: %v", err)
	}
	if listener.Close() != nil {
		t.Fatal("authority listener close")
	}
	if _, err := sentinel.Accept(); !errors.Is(err, net.ErrClosed) {
		t.Fatal("closing the router listener left the namespace listener open")
	}
	want := rootlessnetns.Config{Address: netip.MustParseAddrPort("172.17.0.1:18080"), DockerSocket: wire.DockerSocket, HelperExecutable: helper}
	if len(calls) != 1 || calls[0] != want {
		t.Fatalf("namespace listener config drift: %+v", calls)
	}
	rootlessListen = func(context.Context, rootlessnetns.Config) (net.Listener, error) { return nil, errors.New("refused") }
	calls = nil
	if listener, err := listenRouter(t.Context(), config); err == nil || listener != nil {
		t.Fatal("namespace refusal became a listener")
	}
}

type precheckSeams struct {
	gatewayCalls, precheckCalls int
	sockets                     []string
	configs                     []rootlessnetns.Config
}

// stubPrecheck replaces the configuration loader's executable check and both
// read-only precheck probes for the duration of a test.
func stubPrecheck(t *testing.T, gateway string, gatewayErr, precheckErr error) *precheckSeams {
	t.Helper()
	seams := &precheckSeams{}
	helper := rootlessHelper(t)
	previousLoad, previousGateway, previousPrecheck := loadRuntimeConfig, daemonBridgeGateway, rootlessPrecheck
	t.Cleanup(func() {
		loadRuntimeConfig, daemonBridgeGateway, rootlessPrecheck = previousLoad, previousGateway, previousPrecheck
	})
	loadRuntimeConfig = func(path string) (*runtimeConfig, error) { return loadConfigChecked(path, rootlessExecutable(helper)) }
	daemonBridgeGateway = func(_ context.Context, socket string) (netip.Addr, error) {
		seams.gatewayCalls++
		seams.sockets = append(seams.sockets, socket)
		if gatewayErr != nil {
			return netip.Addr{}, gatewayErr
		}
		return netip.MustParseAddr(gateway), nil
	}
	rootlessPrecheck = func(_ context.Context, config rootlessnetns.Config) error {
		seams.precheckCalls++
		seams.configs = append(seams.configs, config)
		return precheckErr
	}
	return seams
}

func rootlessFixture(t *testing.T) (configWire, string) {
	t.Helper()
	wire, path := fixture(t)
	wire.RouterNamespace, wire.RouterListen = routerNamespaceRootless, "172.17.0.1:18080"
	wire.RouterExpiresAtUnix = routerAuthority()
	writeConfig(t, path, wire)
	return wire, path
}

func TestValidateRunsReadOnlyRootlessRouterPrecheck(t *testing.T) {
	refused := errors.New("refused")
	for name, tc := range map[string]struct {
		gateway                 string
		gatewayErr, precheckErr error
		want                    error
		precheckCalls           int
	}{
		"ready":            {gateway: "172.17.0.1", want: nil, precheckCalls: 1},
		"gateway_mismatch": {gateway: "172.18.0.1", want: ErrConfig},
		"daemon_down":      {gatewayErr: refused, want: ErrConfig},
		"no_rootlesskit":   {gateway: "172.17.0.1", precheckErr: refused, want: ErrConfig, precheckCalls: 1},
	} {
		t.Run(name, func(t *testing.T) {
			wire, path := rootlessFixture(t)
			seams := stubPrecheck(t, tc.gateway, tc.gatewayErr, tc.precheckErr)
			if err := Validate(path); err != tc.want {
				t.Fatalf("Validate = %v, want %v", err, tc.want)
			}
			if seams.gatewayCalls != 1 || seams.precheckCalls != tc.precheckCalls || seams.sockets[0] != wire.DockerSocket {
				t.Fatalf("precheck calls drift: %+v", seams)
			}
			want := rootlessnetns.Config{Address: netip.MustParseAddrPort("172.17.0.1:18080"), DockerSocket: wire.DockerSocket, HelperExecutable: rootlessHelper(t)}
			if tc.precheckCalls == 1 && seams.configs[0] != want {
				t.Fatalf("precheck config drift: %+v", seams.configs[0])
			}
			if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
				t.Fatal("validation consumed attempt")
			}
		})
	}
	t.Run("host_mode_has_no_router_precheck", func(t *testing.T) {
		wire, path := fixture(t)
		writeConfig(t, path, wire)
		seams := stubPrecheck(t, "", refused, refused)
		if err := Validate(path); err != nil || seams.gatewayCalls != 0 || seams.precheckCalls != 0 {
			t.Fatalf("host mode precheck: err=%v %+v", err, seams)
		}
	})
}

// A misconfigured rootless-netns host must refuse before Run consumes the
// one-shot state root, installs its environment or contacts Docker otherwise.
func TestRunRefusesRootlessRouterPrecheckBeforeConsumingAttempt(t *testing.T) {
	refused := errors.New("refused")
	for name, tc := range map[string]struct {
		gateway                 string
		gatewayErr, precheckErr error
	}{
		"gateway_mismatch": {gateway: "172.17.0.2"},
		"daemon_down":      {gatewayErr: refused},
		"detached_netns":   {gateway: "172.17.0.1", precheckErr: refused},
	} {
		t.Run(name, func(t *testing.T) {
			wire, path := rootlessFixture(t)
			stubPrecheck(t, tc.gateway, tc.gatewayErr, tc.precheckErr)
			environment := os.Environ()
			if result, err := Run(t.Context(), path); err != ErrConfig || result != "" {
				t.Fatalf("Run = %q, %v; want configuration refusal", result, err)
			}
			if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
				t.Fatal("refused precheck consumed attempt")
			}
			entries, err := os.ReadDir(wire.StateRoot)
			if err != nil || len(entries) != 0 || !slices.Equal(os.Environ(), environment) {
				t.Fatal("refused precheck prepared the attempt environment")
			}
		})
	}
}

func TestRootlessRouterAuthorityIsBoundedByExpiryAndDeadline(t *testing.T) {
	helper := rootlessHelper(t)
	load := func(t *testing.T, mode string, expires int64) (*runtimeConfig, configWire, error) {
		wire, path := fixture(t)
		wire.RouterNamespace, wire.RouterExpiresAtUnix = mode, expires
		if mode == routerNamespaceRootless {
			wire.RouterListen = "172.17.0.1:18080"
		}
		writeConfig(t, path, wire)
		config, err := loadConfigChecked(path, rootlessExecutable(helper))
		return config, wire, err
	}
	early := time.Now().Add(5 * time.Minute).Unix()
	config, _, err := load(t, routerNamespaceRootless, early)
	if err != nil || !config.router.expires.Equal(time.Unix(early, 0)) {
		t.Fatalf("connectivity expiry before the deadline not used: %v", err)
	}
	config, wire, err := load(t, routerNamespaceRootless, time.Now().Add(2*time.Hour).Unix())
	if err != nil || !config.router.expires.Equal(wire.Harness.Deadline) {
		t.Fatalf("attempt deadline before the connectivity expiry not used: %v", err)
	}
	for name, tc := range map[string]struct {
		mode    string
		expires int64
	}{
		"rootless_missing":    {routerNamespaceRootless, 0},
		"rootless_negative":   {routerNamespaceRootless, -1},
		"rootless_expired":    {routerNamespaceRootless, time.Now().Add(-time.Second).Unix()},
		"rootless_unbounded":  {routerNamespaceRootless, time.Now().Add(maxRouterAuthority + time.Hour).Unix()},
		"host_with_expiry":    {routerNamespaceHost, early},
		"default_with_expiry": {"", early},
	} {
		t.Run(name, func(t *testing.T) {
			if _, wire, err := load(t, tc.mode, tc.expires); err != ErrConfig {
				t.Fatalf("router expiry %d accepted in mode %q", tc.expires, tc.mode)
			} else if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
				t.Fatal("invalid router expiry consumed attempt")
			}
		})
	}
}

// End to end on the real source router: the listener that listenRouter returns
// admits the registered source before the window ends, then closes established
// connections, makes the kernel refuse new ones, and still shuts down cleanly.
func TestRootlessRouterAuthorityEndsCandidateAccessAndClosesCleanly(t *testing.T) {
	local := localPrivateAddress(t)
	probe, err := net.Listen("tcp4", netip.AddrPortFrom(local, 0).String())
	if err != nil {
		t.Fatal(err)
	}
	address := netip.MustParseAddrPort(probe.Addr().String())
	helper := rootlessHelper(t)
	wire, path := fixture(t)
	wire.RouterNamespace, wire.RouterListen, wire.RouterExpiresAtUnix = routerNamespaceRootless, address.String(), routerAuthority()
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, rootlessExecutable(helper))
	if err != nil {
		_ = probe.Close()
		t.Fatalf("fixture rejected: %v", err)
	}
	fakeDocker(t, local.String())
	previous := rootlessListen
	t.Cleanup(func() { rootlessListen = previous })
	// Stand in for the verified namespace listener with a real bound socket.
	rootlessListen = func(context.Context, rootlessnetns.Config) (net.Listener, error) { return probe, nil }
	config.router.expires = time.Now().Add(1500 * time.Millisecond)
	listener, err := listenRouter(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	registry := codingsource.NewRegistry(nil)
	router, err := codingsource.NewRouter(codingsource.RouterConfig{Listener: listener, PublicBaseURL: config.publicBase, Registry: registry, MaxRoutes: 2})
	if err != nil {
		t.Fatal(err)
	}
	binding := codingsource.HarnessBinding{HarnessInstanceID: "authority", AgentArtifactSHA256: strings.Repeat("a", 64), TicketID: "33333333-3333-4333-8333-333333333333", CaseID: "authority", ProfileCapabilityID: "authority", Deadline: time.Now().Add(time.Hour)}
	lease, err := registry.Register(binding, local.String())
	if err != nil {
		t.Fatal(err)
	}
	route, err := router.WorkspacePublisher().Publish(t.Context(), codingcertifier.CapabilityBinding{HarnessInstanceID: binding.HarnessInstanceID, AgentArtifactSHA256: binding.AgentArtifactSHA256, TicketID: binding.TicketID, CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID},
		http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) { response.WriteHeader(http.StatusOK) }))
	if err != nil {
		t.Fatal(err)
	}
	target := strings.Replace(route.URL(), "host.docker.internal", local.String(), 1)
	request := func(conn net.Conn) (int, error) {
		parsed, _ := url.Parse(target)
		if _, err := fmt.Fprintf(conn, "POST %s HTTP/1.1\r\nHost: host.docker.internal:%d\r\nContent-Length: 0\r\n\r\n", parsed.Path, address.Port()); err != nil {
			return 0, err
		}
		response, err := http.ReadResponse(bufio.NewReader(conn), nil)
		if err != nil {
			return 0, err
		}
		_ = response.Body.Close()
		return response.StatusCode, nil
	}
	established, err := net.DialTimeout("tcp4", address.String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer established.Close()
	if code, err := request(established); err != nil || code != http.StatusOK {
		t.Fatalf("registered source refused before the window ended: %d %v", code, err)
	}
	if established.SetReadDeadline(time.Now().Add(10*time.Second)) != nil {
		t.Fatal("deadline")
	}
	_, err = established.Read(make([]byte, 1))
	var timeout net.Error
	if err == nil || errors.As(err, &timeout) && timeout.Timeout() || time.Now().Before(config.router.expires) {
		t.Fatalf("keep-alive connection not closed at the window end: %v", err)
	}
	if conn, err := net.DialTimeout("tcp4", address.String(), time.Second); err == nil {
		_ = conn.Close()
		t.Fatal("router accepted a connection after the window ended")
	}
	if route.Revoke(t.Context()) != nil || route.Close() != nil || lease.Close() != nil {
		t.Fatal("route cleanup")
	}
	closing, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	if err := router.Close(closing); err != nil {
		t.Fatalf("router did not shut down cleanly after the window ended: %v", err)
	}
}

func localPrivateAddress(t *testing.T) netip.Addr {
	t.Helper()
	addresses, err := net.InterfaceAddrs()
	if err != nil {
		t.Fatal(err)
	}
	for _, address := range addresses {
		prefix, err := netip.ParsePrefix(address.String())
		if err == nil && prefix.Addr().Is4() && prefix.Addr().IsPrivate() && !prefix.Addr().IsLoopback() {
			return prefix.Addr()
		}
	}
	t.Skip("no local private IPv4 address")
	return netip.Addr{}
}
