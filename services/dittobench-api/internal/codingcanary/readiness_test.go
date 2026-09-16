package codingcanary

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"
)

const testRuntimeImageDigest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

func placed(context.Context) TopologyCheck {
	return TopologyCheck{RootlessTopology: true, ListenerNamespace: true, ControlSocket: true}
}

func readinessService(t *testing.T, pack PublicPack, readiness func(context.Context) ReadinessCheck) *Service {
	t.Helper()
	return readinessServiceWith(t, pack, placed, testRuntimeImageDigest, readiness)
}

func readinessServiceWith(
	t *testing.T,
	pack PublicPack,
	topology func(context.Context) TopologyCheck,
	digest string,
	readiness func(context.Context) ReadinessCheck,
) *Service {
	t.Helper()
	service, err := New(Config{
		ControlToken:       testToken,
		Backend:            stubBackend{},
		Now:                func() time.Time { return time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC) },
		Pack:               pack,
		Readiness:          readiness,
		Topology:           topology,
		RuntimeImageDigest: digest,
	})
	if err != nil {
		t.Fatal(err)
	}
	return service
}

func getReadiness(t *testing.T, service *Service, method, target string, authorized bool) (*httptest.ResponseRecorder, ReadinessResponse) {
	t.Helper()
	request := httptest.NewRequest(method, target, nil)
	if authorized {
		request.Header.Set("Authorization", "Bearer "+testToken)
	}
	response := httptest.NewRecorder()
	service.ReadinessHandler().ServeHTTP(response, request)
	var decoded ReadinessResponse
	if response.Code == http.StatusOK {
		if err := json.Unmarshal(response.Body.Bytes(), &decoded); err != nil {
			t.Fatal(err)
		}
	}
	if response.Header().Get("Cache-Control") != "no-store" || response.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("headers=%v", response.Header())
	}
	return response, decoded
}

func TestReadinessRequiresTheCanaryBearerAndIsReadOnly(t *testing.T) {
	calls := 0
	service := readinessService(t, PublicPack{}, func(context.Context) ReadinessCheck {
		calls++
		return ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true}
	})
	if response, _ := getReadiness(t, service, http.MethodGet, ReadinessPath, false); response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthorized status=%d", response.Code)
	}
	if response, _ := getReadiness(t, service, http.MethodPost, ReadinessPath, true); response.Code != http.StatusMethodNotAllowed {
		t.Fatalf("post status=%d", response.Code)
	}
	if response, _ := getReadiness(t, service, http.MethodGet, ReadinessPath+"?probe=1", true); response.Code != http.StatusNotFound {
		t.Fatalf("query status=%d", response.Code)
	}
	if calls != 0 {
		t.Fatalf("rejected requests probed the executor %d times", calls)
	}
}

func TestReadinessFailsClosedUntilPackDaemonAndImageAreAllReady(t *testing.T) {
	root := copyImageShapedPack(t)
	pack, err := LoadPublicPack(root)
	if err != nil {
		t.Fatal(err)
	}
	ready := func(context.Context) ReadinessCheck { return ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true} }
	for name, test := range map[string]struct {
		pack      PublicPack
		topology  func(context.Context) TopologyCheck
		digest    string
		readiness func(context.Context) ReadinessCheck
		ready     bool
		failure   string
	}{
		// A scorer without the host certification service (the Compose stack)
		// has no placement proof and is never ready.
		"no topology probe": {pack: pack, digest: testRuntimeImageDigest, readiness: ready, failure: "rootless_topology"},
		"rootful or detached daemon": {
			pack: pack, digest: testRuntimeImageDigest, readiness: ready, failure: "rootless_topology",
			topology: func(context.Context) TopologyCheck {
				return TopologyCheck{ListenerNamespace: true, ControlSocket: true}
			},
		},
		"listener outside the rootless namespace": {
			pack: pack, digest: testRuntimeImageDigest, readiness: ready, failure: "listener_namespace",
			topology: func(context.Context) TopologyCheck {
				return TopologyCheck{RootlessTopology: true, ControlSocket: true}
			},
		},
		"swapped control socket": {
			pack: pack, digest: testRuntimeImageDigest, readiness: ready, failure: "control_socket",
			topology: func(context.Context) TopologyCheck {
				return TopologyCheck{RootlessTopology: true, ListenerNamespace: true}
			},
		},
		"topology probe past its deadline": {
			pack: pack, digest: testRuntimeImageDigest, readiness: ready, failure: "rootless_topology",
			topology: func(ctx context.Context) TopologyCheck {
				<-ctx.Done()
				return TopologyCheck{RootlessTopology: true, ListenerNamespace: true, ControlSocket: true}
			},
		},
		"runtime image tag instead of digest": {
			pack: pack, topology: placed, digest: "latest", readiness: ready, failure: "runtime_image",
		},
		"uppercase runtime image digest": {
			pack: pack, topology: placed, digest: "sha256:" + strings.Repeat("A", 64), readiness: ready, failure: "runtime_image",
		},
		"no pack": {
			pack: PublicPack{}, topology: placed, digest: testRuntimeImageDigest, readiness: ready, failure: "pack",
		},
		"no probe": {pack: pack, topology: placed, digest: testRuntimeImageDigest, failure: "executor_daemon"},
		"rootful or unlabelled daemon": {
			pack: pack, topology: placed, digest: testRuntimeImageDigest,
			readiness: func(context.Context) ReadinessCheck { return ReadinessCheck{RuntimeImage: true} },
			failure:   "executor_daemon",
		},
		"missing runtime image": {
			pack: pack, topology: placed, digest: testRuntimeImageDigest,
			readiness: func(context.Context) ReadinessCheck { return ReadinessCheck{ExecutorDaemon: true} },
			failure:   "runtime_image",
		},
		"ready": {pack: pack, topology: placed, digest: testRuntimeImageDigest, readiness: ready, ready: true},
	} {
		t.Run(name, func(t *testing.T) {
			timeout := test.topology
			ctxService := readinessServiceWith(t, test.pack, timeout, test.digest, test.readiness)
			if name == "topology probe past its deadline" {
				// Keep the test fast: the handler's own deadline is 20 seconds.
				ctx, cancel := context.WithTimeout(t.Context(), 10*time.Millisecond)
				defer cancel()
				decoded := ctxService.readinessResult(ctx)
				if decoded.Ready || decoded.Failure != test.failure || decoded.RootlessTopologyReady {
					t.Fatalf("readiness=%+v", decoded)
				}
				return
			}
			response, decoded := getReadiness(t, ctxService, http.MethodGet, ReadinessPath, true)
			if response.Code != http.StatusOK {
				t.Fatalf("status=%d", response.Code)
			}
			if decoded.Schema != ReadinessSchema || decoded.CodingContractVersion != 1 || decoded.WeightEligible ||
				decoded.Ready != test.ready || decoded.Failure != test.failure {
				t.Fatalf("readiness=%+v", decoded)
			}
			if !test.ready && decoded.Ready {
				t.Fatalf("not-ready response reported ready: %+v", decoded)
			}
			if !test.ready {
				// Each flag requires every earlier one: the failed step and all
				// later steps report false even when their own probe said true.
				flags := []bool{
					decoded.PackLoaded, decoded.RootlessTopologyReady, decoded.ListenerNamespaceReady,
					decoded.ControlSocketReady, decoded.ExecutorDaemonReady, decoded.RuntimeImageReady,
				}
				order := []string{"pack", "rootless_topology", "listener_namespace", "control_socket", "executor_daemon", "runtime_image"}
				failed := slices.Index(order, decoded.Failure)
				if failed < 0 || slices.Contains(flags[failed:], true) || slices.Contains(flags[:failed], false) {
					t.Fatalf("readiness flags do not stop at failure %q: %+v", decoded.Failure, decoded)
				}
			}
			if test.ready && (!decoded.PackLoaded || !decoded.RootlessTopologyReady || !decoded.ListenerNamespaceReady ||
				!decoded.ControlSocketReady || decoded.RuntimeImageDigest != testRuntimeImageDigest ||
				!decoded.ExecutorDaemonReady || !decoded.RuntimeImageReady ||
				decoded.CanaryManifestSHA256 != pack.CanaryManifestSHA256 || decoded.RunnerPlanSHA256 != pack.RunnerPlanSHA256 ||
				decoded.GraderPlanSHA256 != pack.GraderPlanSHA256 || decoded.ResourceProfileSHA256 != pack.ResourceProfileSHA256 ||
				decoded.InferencePolicySHA256 != pack.InferencePolicySHA256) {
				t.Fatalf("ready response is incomplete: %+v", decoded)
			}
		})
	}
}

func TestReadinessReverifiesThePackOnEveryProbe(t *testing.T) {
	root := copyImageShapedPack(t)
	pack, err := LoadPublicPack(root)
	if err != nil {
		t.Fatal(err)
	}
	probes := 0
	service := readinessService(t, pack, func(context.Context) ReadinessCheck {
		probes++
		return ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true}
	})
	if _, decoded := getReadiness(t, service, http.MethodGet, ReadinessPath, true); !decoded.Ready {
		t.Fatalf("initial readiness=%+v", decoded)
	}
	extra := filepath.Join(pack.VisibleDir, "tests", "conftest.py")
	if err := os.WriteFile(extra, []byte("x = 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, decoded := getReadiness(t, service, http.MethodGet, ReadinessPath, true); decoded.Ready || decoded.Failure != "pack" {
		t.Fatalf("drifted readiness=%+v", decoded)
	}
	if probes != 1 {
		t.Fatalf("drifted pack still probed the executor: probes=%d", probes)
	}
}

func TestReadinessRefusesConcurrentOrClosedProbes(t *testing.T) {
	service := readinessService(t, PublicPack{}, nil)
	service.probe <- struct{}{}
	if response, _ := getReadiness(t, service, http.MethodGet, ReadinessPath, true); response.Code != http.StatusServiceUnavailable {
		t.Fatalf("busy status=%d", response.Code)
	}
	<-service.probe
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
	if response, _ := getReadiness(t, service, http.MethodGet, ReadinessPath, true); response.Code == http.StatusOK {
		t.Fatal("closed service reported readiness")
	}
}

func TestReadinessProbeIsBoundedByItsTimeout(t *testing.T) {
	root := copyImageShapedPack(t)
	pack, err := LoadPublicPack(root)
	if err != nil {
		t.Fatal(err)
	}
	service := readinessService(t, pack, func(ctx context.Context) ReadinessCheck {
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > readinessTimeout {
			t.Errorf("probe deadline=%v ok=%v", deadline, ok)
		}
		return ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true}
	})
	if _, decoded := getReadiness(t, service, http.MethodGet, ReadinessPath, true); !decoded.Ready {
		t.Fatalf("readiness=%+v", decoded)
	}
}

// The shared contract vector is the wire both the scorer and the validator
// runtime are pinned to.
func TestReadinessEncodesTheSharedContractVector(t *testing.T) {
	body, err := os.ReadFile(filepath.Join(repoRoot(t), "packages", "dittobench-coding-contract", "testdata", "coding_certification_canary_readiness_v2.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vector struct {
		Ready    map[string]any `json:"ready"`
		Failures []string       `json:"not_ready_failures"`
	}
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	pack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	service := readinessService(t, pack, func(context.Context) ReadinessCheck {
		return ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true}
	})
	response, _ := getReadiness(t, service, http.MethodGet, ReadinessPath, true)
	var encoded map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &encoded); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(encoded, vector.Ready) {
		t.Fatalf("readiness wire drifted from the contract vector:\n got=%v\nwant=%v", encoded, vector.Ready)
	}
	seen := map[string]bool{}
	for _, check := range []struct {
		pack     PublicPack
		topology func(context.Context) TopologyCheck
		check    ReadinessCheck
	}{
		{PublicPack{}, placed, ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true}},
		{pack, nil, ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true}},
		{pack, func(context.Context) TopologyCheck { return TopologyCheck{RootlessTopology: true} }, ReadinessCheck{}},
		{pack, func(context.Context) TopologyCheck {
			return TopologyCheck{RootlessTopology: true, ListenerNamespace: true}
		}, ReadinessCheck{}},
		{pack, placed, ReadinessCheck{}},
		{pack, placed, ReadinessCheck{ExecutorDaemon: true}},
	} {
		_, decoded := getReadiness(t, readinessServiceWith(t, check.pack, check.topology, testRuntimeImageDigest,
			func(context.Context) ReadinessCheck { return check.check }), http.MethodGet, ReadinessPath, true)
		seen[decoded.Failure] = true
	}
	for _, failure := range vector.Failures {
		if !seen[failure] {
			t.Errorf("failure code %q is never produced", failure)
		}
	}
	if len(seen) != len(vector.Failures) {
		t.Fatalf("failure codes=%v vector=%v", seen, vector.Failures)
	}
}
