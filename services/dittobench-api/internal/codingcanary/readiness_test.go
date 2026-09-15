package codingcanary

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func readinessService(t *testing.T, pack PublicPack, readiness func(context.Context) ReadinessCheck) *Service {
	t.Helper()
	service, err := New(Config{
		ControlToken: testToken,
		Backend:      stubBackend{},
		Now:          func() time.Time { return time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC) },
		Pack:         pack,
		Readiness:    readiness,
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
	for name, test := range map[string]struct {
		pack      PublicPack
		readiness func(context.Context) ReadinessCheck
		ready     bool
		failure   string
	}{
		"no pack": {
			pack:      PublicPack{},
			readiness: func(context.Context) ReadinessCheck { return ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true} },
			failure:   "pack",
		},
		"no probe": {pack: pack, failure: "executor_daemon"},
		"rootful or unlabelled daemon": {
			pack:      pack,
			readiness: func(context.Context) ReadinessCheck { return ReadinessCheck{RuntimeImage: true} },
			failure:   "executor_daemon",
		},
		"missing runtime image": {
			pack:      pack,
			readiness: func(context.Context) ReadinessCheck { return ReadinessCheck{ExecutorDaemon: true} },
			failure:   "runtime_image",
		},
		"ready": {
			pack:      pack,
			readiness: func(context.Context) ReadinessCheck { return ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true} },
			ready:     true,
		},
	} {
		t.Run(name, func(t *testing.T) {
			response, decoded := getReadiness(t, readinessService(t, test.pack, test.readiness), http.MethodGet, ReadinessPath, true)
			if response.Code != http.StatusOK {
				t.Fatalf("status=%d", response.Code)
			}
			if decoded.Schema != ReadinessSchema || decoded.CodingContractVersion != 1 || decoded.WeightEligible ||
				decoded.Ready != test.ready || decoded.Failure != test.failure {
				t.Fatalf("readiness=%+v", decoded)
			}
			if test.ready && (!decoded.PackLoaded || !decoded.ExecutorDaemonReady || !decoded.RuntimeImageReady ||
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
	body, err := os.ReadFile(filepath.Join(repoRoot(t), "packages", "dittobench-coding-contract", "testdata", "coding_certification_canary_readiness_v1.json"))
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
		pack  PublicPack
		check ReadinessCheck
	}{
		{PublicPack{}, ReadinessCheck{ExecutorDaemon: true, RuntimeImage: true}},
		{pack, ReadinessCheck{}},
		{pack, ReadinessCheck{ExecutorDaemon: true}},
	} {
		_, decoded := getReadiness(t, readinessService(t, check.pack, func(context.Context) ReadinessCheck { return check.check }), http.MethodGet, ReadinessPath, true)
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
