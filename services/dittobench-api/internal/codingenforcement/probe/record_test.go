package probe

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
)

// goldenRecordSHA256 is pinned identically in catalog/canonical_test.go and the
// Python evidence tests. Reassembling it here proves the runner's record
// assembly is byte-for-byte the canonical evidence form both sides hash.
const goldenRecordSHA256 = "8ed4ce9e831a20ae9bee4c60a865c44a3340c6098d4074615938c7446d8aff6c"

func goldenEnv() Env {
	return Env{
		Host: HostBinding{
			MachineIDSHA256:      "4be2df8ab9c9d66c3041722a09fe777d22fe87525f04937b3a4685433b9a0a9d",
			BootID:               "11111111-2222-3333-4444-555555555555",
			KernelRelease:        "6.12.43+deb13-cloud-amd64",
			DaemonIdentitySHA256: "1a6167c7a37a205c2503beadd64d72e9d1860b63a790faead47de32998ec187a",
			RouterNamespace:      "rootless-netns",
			Subordinate:          catalog.SubordinateIDs{UIDStart: 100000, UIDCount: 65536, GIDStart: 100000, GIDCount: 65536},
		},
		Release: ReleaseBinding{
			SourceRevision:        "0123456789abcdef0123456789abcdef01234567",
			ReleaseManifestSHA256: "707a400adad19b9fba18e0645815bf9e8516e3b12823789b387b033e211bbe1e",
			RuntimeArchiveSHA256:  "21b0e33f316915551cca77e617b073b99b53ac813b450f6ae0ff26025a086029",
			ImageApprovalSHA256: map[string]string{
				"go":     "637ca05baaaef19a322b67dcf2bd14e682b7d47b952b86fe0046a00ad9e8e23d",
				"node":   "b776bb8409e1af94cc515f5a69257d08dc7ee591d1524397fc0f95a24db64d2a",
				"python": "7447bc93097540f14582364956fe8b11a7e23743acc7d187f6c8487c83c2d671",
				"rust":   "5a6b28a505595c39ce76f0d4c319d107175351873cba93977750ad0e25b3998f",
			},
		},
		Tools: map[string]string{
			"catalog_sha256":       "652f55016243bf1b9f1bbea46d5749ef892dbe394e46de9d66ab1aacf0b4af57",
			"collector_sha256":     "0736fd5b7cc7ab7dfe821d3a17f93f2634497770232486155c9c881321c4d22c",
			"evidence_tool_sha256": "49e460f2f099b092db8e060bf175d3f49eac8431d8be669bd35e91c08727edf5",
			"fixtures_sha256":      "5b4818f81c7256e686ba2e0537258c852c6f5c3e646e84f9625f8744ede6a8f8",
			"runner_sha256":        "527aa9f431539da8e151d5434d1d5e611d973f601d8e970790882624554146b0",
		},
		ProfileInputs:                map[string]string{"execution_profile_sha256": "f94c52f808d5df347becd9fd4cc184a15d5b6c181140c897467405d769c50bae"},
		PreCollectionPreflightSHA256: "82998cdb86201643e69090056ad333315285b557d7fb3447ca040a525f977027",
		Preconditions: map[string]any{
			"custody_active": false, "custody_socket_present": false,
			"daemon_containers": int64(0), "daemon_job_networks": int64(0), "worker_active": false,
		},
		Residue: map[string]any{
			"containers": int64(0), "custody_socket_present": false, "job_networks": int64(0),
			"processes": int64(0), "volumes": int64(0), "worker_active": false,
		},
		StartedAtUnix:   2000000000,
		CompletedAtUnix: 2000000017,
	}
}

func absent() map[string]any {
	return map[string]any{"containers": int64(0), "networks": int64(0), "processes": int64(0), "volumes": int64(0)}
}

func outcome(value string) map[string]any { return map[string]any{"outcome": value} }

func goldenCleanupPhases() []Phase {
	at := func(base int64) (int64, int64) { return base, base + 1 }
	phase := func(name string, base int64, obs ...Observation) Phase {
		start, end := at(base)
		return Phase{Name: name, StartedAtUnix: start, CompletedAtUnix: end, Observations: obs}
	}
	host := func(id string, observed map[string]any) Observation {
		return Observation{ID: id, Observed: observed}
	}
	return []Phase{
		phase("normal_stop", 2000000001, host("cleanup.normal_stop.absent", absent())),
		phase("partial_start", 2000000003, host("cleanup.partial_start.absent", absent())),
		phase("timeout", 2000000005, host("cleanup.timeout.absent", absent())),
		phase("oom", 2000000007, host("cleanup.oom.absent", absent())),
		phase("escaped_setsid", 2000000009, host("cleanup.escaped_setsid.absent", absent())),
		phase("runner_sigterm", 2000000011, host("cleanup.runner_sigterm.absent", absent())),
		phase("runner_sigkill", 2000000013,
			host("cleanup.runner_sigkill.reconciled_absent", absent()),
			host("cleanup.runner_sigkill.journal_ids_only", outcome("journal_ids_only")),
			host("cleanup.runner_sigkill.sentinel_network", outcome("present")),
		),
		phase("rerun", 2000000015, host("cleanup.rerun.consumed_marker", outcome("refused"))),
	}
}

func TestSyntheticRecordReproducesTheGoldenRecordBytes(t *testing.T) {
	cat, err := catalog.Load()
	if err != nil {
		t.Fatal(err)
	}
	result, err := assembleSyntheticRecord(cat, goldenEnv(), "cleanup_recovery", catalog.Endpoints{}, goldenCleanupPhases())
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(result.Canonical)
	if got := hex.EncodeToString(sum[:]); got != goldenRecordSHA256 {
		t.Fatalf("assembled record digest = %s, want the pinned golden %s\n%s", got, goldenRecordSHA256, result.Canonical)
	}
	if result.Passed != result.Total || result.Total != 10 || len(result.Unmatched) != 0 {
		t.Fatalf("golden record must fully match: %+v", result)
	}
}

func TestSyntheticRecordRecomputesMatchedFromObservations(t *testing.T) {
	cat, err := catalog.Load()
	if err != nil {
		t.Fatal(err)
	}
	phases := goldenCleanupPhases()
	// A weakened absence observation (a container survived) must be recomputed
	// as unmatched, never trusted from the runner.
	phases[0].Observations[0].Observed = map[string]any{"containers": int64(1), "networks": int64(0), "processes": int64(0), "volumes": int64(0)}
	result, err := assembleSyntheticRecord(cat, goldenEnv(), "cleanup_recovery", catalog.Endpoints{}, phases)
	if err != nil {
		t.Fatal(err)
	}
	if result.Passed != 9 || result.Total != 10 || len(result.Unmatched) != 1 || result.Unmatched[0] != "cleanup.normal_stop.absent" {
		t.Fatalf("weakened observation was not recomputed as unmatched: %+v", result)
	}
}

func TestSyntheticRecordRefusesAnIncompleteCollection(t *testing.T) {
	cat, err := catalog.Load()
	if err != nil {
		t.Fatal(err)
	}
	phases := goldenCleanupPhases()
	phases[7].Observations = nil // drop the rerun consumed marker
	_, err = assembleSyntheticRecord(cat, goldenEnv(), "cleanup_recovery", catalog.Endpoints{}, phases)
	if err == nil {
		t.Fatal("assembling an incomplete collection must fail")
	}
}

func TestSyntheticRecordRefusesAnUnexpectedProbe(t *testing.T) {
	cat, err := catalog.Load()
	if err != nil {
		t.Fatal(err)
	}
	phases := goldenCleanupPhases()
	phases[0].Observations = append(phases[0].Observations, Observation{ID: "cleanup.normal_stop.extra", Observed: absent()})
	_, err = assembleSyntheticRecord(cat, goldenEnv(), "cleanup_recovery", catalog.Endpoints{}, phases)
	if err == nil {
		t.Fatal("assembling an unexpected probe must fail")
	}
}

func TestSyntheticRecordRefusesMissingPreconditionsOrResidue(t *testing.T) {
	cat, err := catalog.Load()
	if err != nil {
		t.Fatal(err)
	}
	for _, strip := range []func(*Env){
		func(env *Env) { env.Preconditions = nil },
		func(env *Env) { env.Residue = nil },
	} {
		env := goldenEnv()
		strip(&env)
		if _, err := assembleSyntheticRecord(cat, env, "cleanup_recovery", catalog.Endpoints{}, goldenCleanupPhases()); err == nil {
			t.Fatal("unmeasured preconditions or residue must never default to clear")
		}
	}
}
