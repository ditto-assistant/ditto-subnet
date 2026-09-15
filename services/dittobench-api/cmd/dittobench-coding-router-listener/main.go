// The router listener helper is started only by the hosted worker, through
// nsenter, inside the rootless daemon's pinned user and network namespaces. It
// returns one listening socket over fd 3 and exits. It is not a service.
package main

import (
	"os"

	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
)

func main() {
	os.Exit(rootlessnetns.RunHelper(os.Args[1:]))
}
