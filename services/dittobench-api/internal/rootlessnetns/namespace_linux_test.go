package rootlessnetns

import (
	"context"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
)

func validFacts() (uint32, selfFacts, processFacts, processFacts, uint32) {
	const euid = 1001
	self := selfFacts{user: nsID{dev: 4, ino: 100}, net: nsID{dev: 4, ino: 200}}
	child := processFacts{
		uids: [4]uint32{euid, euid, euid, euid}, user: nsID{dev: 4, ino: 101}, userOwner: euid,
		userParent: self.user, net: nsID{dev: 4, ino: 201}, netOwner: nsID{dev: 4, ino: 101},
	}
	daemon := child
	return euid, self, child, daemon, euid
}

func TestValidTopologyRequiresEveryNamespaceRelation(t *testing.T) {
	euid, self, child, daemon, peer := validFacts()
	if !validTopology(euid, self, child, daemon, peer) {
		t.Fatal("RootlessKit topology rejected")
	}
	other := nsID{dev: 4, ino: 999}
	for name, mutate := range map[string]func(*uint32, *selfFacts, *processFacts, *processFacts, *uint32){
		"zero_self_user":      func(_ *uint32, s *selfFacts, _, _ *processFacts, _ *uint32) { s.user = nsID{} },
		"zero_self_net":       func(_ *uint32, s *selfFacts, _, _ *processFacts, _ *uint32) { s.net = nsID{} },
		"zero_child_user":     func(_ *uint32, _ *selfFacts, c, d *processFacts, _ *uint32) { c.user, d.user = nsID{}, nsID{} },
		"zero_child_net":      func(_ *uint32, _ *selfFacts, c, d *processFacts, _ *uint32) { c.net, d.net = nsID{}, nsID{} },
		"host_user_namespace": func(_ *uint32, s *selfFacts, c, d *processFacts, _ *uint32) { c.user, d.user = s.user, s.user },
		"host_network":        func(_ *uint32, s *selfFacts, c, d *processFacts, _ *uint32) { c.net, d.net = s.net, s.net },
		// Observed with Docker 29.8.0 and RootlessKit 3.1.0: the daemon stays in
		// the host network namespace; the child holds the detached namespace.
		"detached_netns": func(_ *uint32, s *selfFacts, _, d *processFacts, _ *uint32) {
			d.net, d.netOwner = s.net, other
		},
		"foreign_owner":       func(_ *uint32, _ *selfFacts, c, _ *processFacts, _ *uint32) { c.userOwner++ },
		"nested_user":         func(_ *uint32, _ *selfFacts, c, _ *processFacts, _ *uint32) { c.userParent = other },
		"foreign_net_owner":   func(_ *uint32, _ *selfFacts, c, _ *processFacts, _ *uint32) { c.netOwner = other },
		"child_real_uid":      func(_ *uint32, _ *selfFacts, c, _ *processFacts, _ *uint32) { c.uids[0]++ },
		"child_effective_uid": func(_ *uint32, _ *selfFacts, c, _ *processFacts, _ *uint32) { c.uids[1]++ },
		"child_saved_uid":     func(_ *uint32, _ *selfFacts, c, _ *processFacts, _ *uint32) { c.uids[2]++ },
		"child_fs_uid":        func(_ *uint32, _ *selfFacts, c, _ *processFacts, _ *uint32) { c.uids[3]++ },
		"daemon_other_user":   func(_ *uint32, _ *selfFacts, _, d *processFacts, _ *uint32) { d.user = other },
		"daemon_other_net":    func(_ *uint32, _ *selfFacts, _, d *processFacts, _ *uint32) { d.net = other },
		"daemon_net_owner":    func(_ *uint32, _ *selfFacts, _, d *processFacts, _ *uint32) { d.netOwner = other },
		"daemon_uid":          func(_ *uint32, _ *selfFacts, _, d *processFacts, _ *uint32) { d.uids[1]++ },
		"socket_peer_uid":     func(_ *uint32, _ *selfFacts, _, _ *processFacts, p *uint32) { *p = 0 },
		"worker_uid_differs":  func(e *uint32, _ *selfFacts, _, _ *processFacts, _ *uint32) { *e = 0 },
		"daemon_host_network": func(_ *uint32, s *selfFacts, _, d *processFacts, _ *uint32) { d.net = s.net },
		"daemon_host_userns":  func(_ *uint32, s *selfFacts, _, d *processFacts, _ *uint32) { d.user = s.user },
		"parent_is_itself":    func(_ *uint32, _ *selfFacts, c, _ *processFacts, _ *uint32) { c.userParent = c.user },
	} {
		t.Run(name, func(t *testing.T) {
			e, s, c, d, p := validFacts()
			mutate(&e, &s, &c, &d, &p)
			if validTopology(e, s, c, d, p) {
				t.Fatal("invalid namespace topology accepted")
			}
		})
	}
}

func stateFixture(t *testing.T, body string, mode os.FileMode) (string, string) {
	t.Helper()
	root, err := os.MkdirTemp("", "rootless-run-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	if os.Chmod(root, 0o755) != nil {
		t.Fatal("run root mode")
	}
	state := filepath.Join(root, strconv.Itoa(os.Geteuid()), stateDirName)
	if os.MkdirAll(state, 0o700) != nil || os.Chmod(filepath.Dir(state), 0o700) != nil {
		t.Fatal("state fixture")
	}
	file := filepath.Join(state, childPIDName)
	if os.WriteFile(file, []byte(body), 0o600) != nil || os.Chmod(file, mode) != nil {
		t.Fatal("child pid fixture")
	}
	return root, file
}

func TestReadChildPIDAcceptsRootlessKitStateFile(t *testing.T) {
	for _, mode := range []os.FileMode{0o444, 0o400, 0o440} {
		root, _ := stateFixture(t, "4242", mode)
		if pid, err := readChildPID(root, os.Geteuid()); err != nil || pid != 4242 {
			t.Fatalf("mode %o: pid %d err %v", mode, pid, err)
		}
	}
}

func TestReadChildPIDRefusesUnsafeOrMalformedState(t *testing.T) {
	euid := os.Geteuid()
	for name, change := range map[string]func(t *testing.T, root, file string){
		"newline":           func(t *testing.T, _, file string) { rewrite(t, file, "4242\n") },
		"leading_zero":      func(t *testing.T, _, file string) { rewrite(t, file, "04242") },
		"init":              func(t *testing.T, _, file string) { rewrite(t, file, "1") },
		"above_pid_max":     func(t *testing.T, _, file string) { rewrite(t, file, "4194305") },
		"non_digit":         func(t *testing.T, _, file string) { rewrite(t, file, "42a2") },
		"signed":            func(t *testing.T, _, file string) { rewrite(t, file, "+4242") },
		"empty":             func(t *testing.T, _, file string) { rewrite(t, file, "") },
		"oversized":         func(t *testing.T, _, file string) { rewrite(t, file, "12345678") },
		"owner_writable":    func(t *testing.T, _, file string) { chmod(t, file, 0o644) },
		"group_writable":    func(t *testing.T, _, file string) { chmod(t, file, 0o464) },
		"executable":        func(t *testing.T, _, file string) { chmod(t, file, 0o544) },
		"hard_link":         func(t *testing.T, _, file string) { link(t, file, file+"-copy") },
		"missing":           func(t *testing.T, _, file string) { remove(t, file) },
		"file_symlink":      func(t *testing.T, _, file string) { replaceWithSymlink(t, file) },
		"state_symlink":     func(t *testing.T, _, file string) { replaceWithSymlink(t, filepath.Dir(file)) },
		"runtime_symlink":   func(t *testing.T, _, file string) { replaceWithSymlink(t, filepath.Dir(filepath.Dir(file))) },
		"state_writable":    func(t *testing.T, _, file string) { chmod(t, filepath.Dir(file), 0o770) },
		"runtime_shared":    func(t *testing.T, _, file string) { chmod(t, filepath.Dir(filepath.Dir(file)), 0o750) },
		"run_root_writable": func(t *testing.T, root, _ string) { chmod(t, root, 0o777) },
		"fifo": func(t *testing.T, _, file string) {
			remove(t, file)
			if syscall.Mkfifo(file, 0o400) != nil {
				t.Fatal("fifo fixture")
			}
		},
		"directory": func(t *testing.T, _, file string) {
			remove(t, file)
			if os.Mkdir(file, 0o500) != nil {
				t.Fatal("directory fixture")
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			root, file := stateFixture(t, "4242", 0o444)
			change(t, root, file)
			if pid, err := readChildPID(root, euid); err == nil {
				t.Fatalf("unsafe state accepted as pid %d", pid)
			}
		})
	}
	root, _ := stateFixture(t, "4242", 0o444)
	for _, bad := range []string{"relative/run", root + "/", root + "/../" + filepath.Base(root)} {
		if _, err := readChildPID(bad, euid); err == nil {
			t.Fatalf("noncanonical run root %q accepted", bad)
		}
	}
	if _, err := readChildPID(root, euid+1); err == nil {
		t.Fatal("another UID's state path accepted")
	}
}

func rewrite(t *testing.T, file, body string) {
	t.Helper()
	chmod(t, file, 0o600)
	if os.WriteFile(file, []byte(body), 0o600) != nil {
		t.Fatal("rewrite")
	}
	chmod(t, file, 0o444)
}

func chmod(t *testing.T, path string, mode os.FileMode) {
	t.Helper()
	if os.Chmod(path, mode) != nil {
		t.Fatal("chmod")
	}
}

func link(t *testing.T, oldname, newname string) {
	t.Helper()
	if os.Link(oldname, newname) != nil {
		t.Fatal("link")
	}
}

func remove(t *testing.T, path string) {
	t.Helper()
	if os.Remove(path) != nil {
		t.Fatal("remove")
	}
}

func replaceWithSymlink(t *testing.T, path string) {
	t.Helper()
	moved := path + "-real"
	if os.Rename(path, moved) != nil || os.Symlink(moved, path) != nil {
		t.Fatal("symlink fixture")
	}
}

func TestParseStatusUIDsRequiresOneExactLine(t *testing.T) {
	uids, err := parseStatusUIDs([]byte("Name:\texe\nUid:\t1001\t1001\t1001\t1001\nGid:\t1001\t1001\t1001\t1001\n"))
	if err != nil || uids != [4]uint32{1001, 1001, 1001, 1001} {
		t.Fatal("valid status rejected")
	}
	for _, body := range []string{
		"Name:\texe\n",
		"Uid:\t1001\t1001\t1001\n",
		"Uid:\t1001\t1001\t1001\t1001\t1001\n",
		"Uid:\t1001\t1001\t1001\t+1001\n",
		"Uid:\t1001\t1001\t1001\t01001\n",
		"Uid:\t1001\t1001\t1001\t4294967296\n",
		"Uid:\t1001\t1001\t1001\t1001\nUid:\t1001\t1001\t1001\t1001\n",
	} {
		if _, err := parseStatusUIDs([]byte(body)); err == nil {
			t.Fatalf("malformed status accepted: %q", body)
		}
	}
}

func TestInspectProcessRefusesHostAndExitedProcesses(t *testing.T) {
	self, err := inspectSelf("/proc")
	if err != nil {
		t.Fatalf("self namespaces: %v", err)
	}
	// In the initial user namespace NS_GET_PARENT refuses; anywhere else the
	// process shares this test's namespaces, so the topology must refuse.
	if pinned, err := inspectProcess("/proc", os.Getpid()); err == nil {
		defer pinned.Close()
		euid := uint32(os.Geteuid())
		if validTopology(euid, self, pinned.facts, pinned.facts, euid) {
			t.Fatal("host process accepted as RootlessKit child")
		}
	}
	command := exec.Command("/bin/true")
	if command.Run() != nil {
		t.Skip("no /bin/true")
	}
	if _, err := inspectProcess("/proc", command.ProcessState.Pid()); err == nil {
		t.Fatal("exited process inspected")
	}
	for _, pid := range []int{-1, 0, 1, maxChildPID + 1} {
		if _, err := inspectProcess("/proc", pid); err == nil {
			t.Fatalf("pid %d inspected", pid)
		}
	}
}

func TestDaemonPeerReportsListeningProcessCredentials(t *testing.T) {
	root, err := os.MkdirTemp("", "rootless-peer-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	socket := filepath.Join(root, "docker.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			_ = conn.Close()
		}
	}()
	pid, uid, err := daemonPeer(t.Context(), socket)
	if err != nil || pid != os.Getpid() || uid != uint32(os.Geteuid()) {
		t.Fatalf("peer credentials pid=%d uid=%d err=%v", pid, uid, err)
	}
	for _, bad := range []string{"docker.sock", socket + "/", filepath.Join(root, "missing.sock")} {
		if _, _, err := daemonPeer(t.Context(), bad); err == nil {
			t.Fatalf("peer socket %q accepted", bad)
		}
	}
}

// Without RootlessKit, Listen must refuse after reading state and before
// starting nsenter. The "nsenter" here records any execution.
func TestListenRefusesHostTopologyBeforeStartingNsenter(t *testing.T) {
	root, _ := stateFixture(t, strconv.Itoa(os.Getpid()), 0o444)
	marker := filepath.Join(root, "nsenter-ran")
	nsenter := filepath.Join(root, "nsenter")
	if os.WriteFile(nsenter, []byte("#!/bin/sh\ntouch "+marker+"\nexit 1\n"), 0o700) != nil {
		t.Fatal("nsenter fixture")
	}
	socket := filepath.Join(root, "docker.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	sys := system{runRoot: root, procRoot: "/proc", nsenter: nsenter}
	valid := Config{Address: netip.MustParseAddrPort("172.17.0.1:18080"), DockerSocket: socket, HelperExecutable: "/approved/" + HelperExecutableName}
	if _, err := listen(t.Context(), valid, sys); err == nil {
		t.Fatal("host topology produced a listener")
	}
	if precheck(t.Context(), valid, sys) == nil {
		t.Fatal("host topology passed the precheck")
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	for name, config := range map[string]Config{
		"loopback":        {Address: netip.MustParseAddrPort("127.0.0.1:18080"), DockerSocket: socket, HelperExecutable: valid.HelperExecutable},
		"public":          {Address: netip.MustParseAddrPort("8.8.8.8:18080"), DockerSocket: socket, HelperExecutable: valid.HelperExecutable},
		"ipv6":            {Address: netip.MustParseAddrPort("[fd00::1]:18080"), DockerSocket: socket, HelperExecutable: valid.HelperExecutable},
		"privileged_port": {Address: netip.MustParseAddrPort("172.17.0.1:80"), DockerSocket: socket, HelperExecutable: valid.HelperExecutable},
		"zero":            {DockerSocket: socket, HelperExecutable: valid.HelperExecutable},
		"relative_helper": {Address: valid.Address, DockerSocket: socket, HelperExecutable: HelperExecutableName},
		"relative_socket": {Address: valid.Address, DockerSocket: "docker.sock", HelperExecutable: valid.HelperExecutable},
	} {
		if _, err := listen(t.Context(), config, sys); err == nil {
			t.Fatalf("%s config accepted", name)
		}
		if precheck(t.Context(), config, sys) == nil {
			t.Fatalf("%s config passed the precheck", name)
		}
	}
	if _, err := listen(cancelled, valid, sys); err == nil {
		t.Fatal("cancelled context accepted")
	}
	//nolint:staticcheck // nil context must be refused, not dereferenced.
	if _, err := listen(nil, valid, sys); err == nil {
		t.Fatal("nil context accepted")
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("nsenter ran without a validated RootlessKit topology")
	}
}
