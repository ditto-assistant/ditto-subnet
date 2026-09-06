package sandbox

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func TestRetainedStartFailureKeepsExactCleanupIdentity(t *testing.T) {
	for _, phase := range []string{"network", "run"} {
		t.Run(phase, func(t *testing.T) {
			d := NewLocalDocker()
			d.EgressNetwork = "capability-only"
			var network, container string
			d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
				switch args[0] {
				case "network":
					if args[1] != "create" {
						t.Fatal("failed start performed unverifiable cleanup")
					}
					network = args[len(args)-1]
					if phase == "network" {
						return nil, errors.New("ambiguous network creation")
					}
					return []byte(network), nil
				case "run":
					for i := 0; i+1 < len(args); i++ {
						if args[i] == "--name" {
							container = args[i+1]
						}
					}
					return nil, errors.New("ambiguous start")
				default:
					t.Fatalf("unexpected command %v", args)
					return nil, nil
				}
			}
			h, err := d.RunRetainingFailedHandle(t.Context(), "operator-image:latest", nil)
			if err == nil || h == nil || h.NetworkName != network || h.ImageRef != "operator-image:latest" {
				t.Fatal("lost cleanup authority")
			}
			if phase == "run" && h.ContainerID != container {
				t.Fatal("wrong container cleanup authority")
			}
			if h.ContainerID != "dittobench-"+strings.TrimPrefix(network, "ditto-job-") {
				t.Fatal("not exact generated identity")
			}
		})
	}
}
