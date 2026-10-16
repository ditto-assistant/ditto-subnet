// Package rootlessnetns creates the hosted source router's TCP listener inside
// the RootlessKit network namespace of the configured rootless Docker daemon.
//
// A listener in the host namespace cannot bind requests to one container:
// slirp4netns proxies container traffic to host addresses from a host socket,
// so every request arrives with the host's own address as its source. A
// listener on the Docker bridge gateway inside the RootlessKit namespace sees
// the container's bridge address directly.
//
// Go cannot join a user namespace from a multithreaded process. The worker
// therefore validates the RootlessKit child and the daemon, pins their
// namespace descriptors, and starts util-linux nsenter with those descriptors
// to run a one-shot helper. The helper creates only the listening socket and
// returns it over an inherited socketpair with SCM_RIGHTS. The worker verifies
// the received socket's type, state, local address and network namespace
// before serving on it. Every mismatch refuses the listener.
package rootlessnetns
