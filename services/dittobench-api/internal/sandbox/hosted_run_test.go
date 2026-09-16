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

func TestLaunchIntentPrecedesNetworkAndContainerCreation(t *testing.T) {
	d := NewHostedHarnessDocker(HostedHarnessConfig{MemoryLimitBytes: 1 << 30, ScratchLimitBytes: 1 << 28, CPUQuotaMillis: 1000, PidsLimit: 64, EgressNetwork: "restricted"})
	var events []string
	var intent []string
	d.LaunchIntent = func(_ context.Context, run string, containers, networks []string) error {
		events = append(events, "intent")
		intent = append(append([]string{run}, containers...), networks...)
		return nil
	}
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		switch args[0] {
		case "network":
			events = append(events, "network:"+args[len(args)-1])
			return []byte(args[len(args)-1]), nil
		case "run":
			for i := 0; i+1 < len(args); i++ {
				if args[i] == "--name" {
					events = append(events, "run:"+args[i+1])
				}
			}
			return nil, errors.New("start failed")
		}
		t.Fatalf("unexpected command %v", args)
		return nil, nil
	}
	h, _ := d.RunRetainingFailedHandle(t.Context(), "operator-image:latest", nil)
	if h == nil || len(intent) != 3 {
		t.Fatalf("intent = %v", intent)
	}
	identity := intent[0]
	want := []string{"intent", "network:ditto-job-" + identity, "run:dittobench-" + identity}
	if strings.Join(events, ",") != strings.Join(want, ",") || intent[1] != "dittobench-"+identity || intent[2] != "ditto-job-"+identity {
		t.Fatalf("events = %v intent = %v", events, intent)
	}

	events = nil
	d.LaunchIntent = func(context.Context, string, []string, []string) error { return errors.New("journal refused") }
	if h, err := d.RunRetainingFailedHandle(t.Context(), "operator-image:latest", nil); err == nil || h != nil || len(events) != 0 {
		t.Fatalf("launch proceeded without its intent: %v %v %v", h, err, events)
	}
}
