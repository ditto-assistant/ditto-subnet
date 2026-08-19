package main

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
)

// Platform mints the confirmation execution profile in Python and Go
// re-derives every digest in it: the LongMem profile checksum, the frozen
// ablation profile checksum, and the outer profile checksum. Those three
// derivations each marshal the profile's bench_version, so a bench_version
// pinned on one side and carried on the other diverges silently until a
// validator has already claimed the ticket and the run dies as an opaque
// "confirmation LongMem profile checksum mismatch".
//
// These cases assert the agreement on the bytes Python actually writes, for
// every confirmation epoch: the shipped v9 release asset, and the v12 sibling
// the same generator produces. The v12 fixture is test data, not release data
// — see scripts/generate_confirmation_installation.py --bench-version.
func TestPlatformMintedProfilesAgreeWithGoDigests(t *testing.T) {
	for _, testCase := range []struct {
		name              string
		path              string
		expectedBenchVer  int
		expectDeepHistory bool
	}{
		{
			name:              "v9 release asset",
			path:              "../../../../packages/ditto-screening-protocol/ditto_screening_protocol/data/confirmation_execution_profile_v9_shadow.json",
			expectedBenchVer:  9,
			expectDeepHistory: false,
		},
		{
			name:              "v12 generated sibling",
			path:              "testdata/confirmation_execution_profile_v12_shadow.json",
			expectedBenchVer:  12,
			expectDeepHistory: true,
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			raw, err := os.ReadFile(testCase.path)
			if err != nil {
				t.Fatal(err)
			}
			profile, canonical, err := decodeAndValidateConfirmationProfile(json.RawMessage(raw))
			if err != nil {
				// validate() recomputes all three digests; any Python/Go
				// disagreement lands here.
				t.Fatalf("Platform-minted profile rejected by Go: %v", err)
			}
			if len(canonical) == 0 {
				t.Fatal("canonical profile bytes are empty")
			}
			if got := profile.benchVersion(); got != testCase.expectedBenchVer {
				t.Fatalf("bench version = %d, want %d", got, testCase.expectedBenchVer)
			}
			longMem := profile.longMemProfile()
			if err := longMem.Validate(); err != nil {
				t.Fatalf("LongMem profile invalid: %v", err)
			}
			checksum, err := longMem.Checksum()
			if err != nil {
				t.Fatal(err)
			}
			if checksum != profile.LongMemProfileChecksum {
				t.Fatalf("LongMem checksum = %s, Platform minted %s", checksum, profile.LongMemProfileChecksum)
			}
			ablationChecksum, err := ablation.FrozenProfileSHA256(profile.ablationProfile())
			if err != nil {
				t.Fatal(err)
			}
			if ablationChecksum != profile.AblationProfileChecksum {
				t.Fatalf("ablation checksum = %s, Platform minted %s", ablationChecksum, profile.AblationProfileChecksum)
			}
			if !longmemeval.IsOfficialCleanedDataset(longMem) {
				t.Fatal("profile must remain the pinned official cleaned dataset")
			}
			deep := longMem.MinHistorySessions >= longmemeval.V10MinHistorySessions &&
				longMem.MinHistoryBytes >= longmemeval.V10MinHistoryBytes
			if deep != testCase.expectDeepHistory {
				t.Fatalf("deep-history floors present = %v, want %v", deep, testCase.expectDeepHistory)
			}
		})
	}
}
