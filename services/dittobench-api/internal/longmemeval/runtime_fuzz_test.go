package longmemeval

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/google/uuid"
)

func FuzzProjectionNeverLeaksSourceIdentifiers(f *testing.F) {
	f.Add([]byte("0123456789abcdef0123456789abcdef"), uint8(1))
	f.Add([]byte("fedcba9876543210fedcba9876543210"), uint8(64))
	f.Fuzz(func(t *testing.T, keyMaterial []byte, batch uint8) {
		if len(keyMaterial) > 256 {
			keyMaterial = keyMaterial[:256]
		}
		keyDigest := sha256.Sum256(keyMaterial)
		key := keyDigest[:]
		profile, _, dataset := runtimeFixture(t)
		_ = profile
		batchSize := int(batch%8) + 1
		projected, err := ProjectSelectedCases(dataset, key, batchSize)
		if err != nil {
			t.Fatal(err)
		}
		raw := wireJSON(t, projected)
		for _, forbidden := range []string{
			"question_id", "question_type", "answer_session_ids", "has_answer",
			"origin", "provenance", "answer-proof", "private-extract",
			"private-multi", "private-temporal", "private-update", "private-preference",
			"private-abstain",
		} {
			if bytes.Contains(raw, []byte(forbidden)) {
				t.Fatalf("projected payload leaked %q", forbidden)
			}
		}
		for _, item := range projected {
			if _, err := uuid.Parse(item.RunRequest.UserID); err != nil {
				t.Fatal(err)
			}
			if _, err := uuid.Parse(item.RunRequest.CaseID); err != nil {
				t.Fatal(err)
			}
		}
	})
}

func FuzzPairProjectionPreservesNonEmptyTurnContent(f *testing.F) {
	f.Add([]byte{0, 1, 0, 1}, "alpha")
	f.Add([]byte{1, 1, 0, 0, 1}, "beta")
	f.Add([]byte{0}, "gamma")
	f.Fuzz(func(t *testing.T, roles []byte, contentSeed string) {
		if len(roles) == 0 || len(roles) > 64 || len(contentSeed) > 128 {
			t.Skip()
		}
		turns := make([]DatasetTurn, 0, len(roles))
		wantContent := make(map[string]struct{}, len(roles))
		for index, rawRole := range roles {
			role := "user"
			if rawRole%2 == 1 {
				role = "assistant"
			}
			content := contentSeed + "-turn-" + string(rune('A'+index%26)) + strings.Repeat("x", index/26)
			turns = append(turns, DatasetTurn{Role: role, Content: content})
			wantContent[content] = struct{}{}
		}
		entry := DatasetCase{
			QuestionID:         "private-fuzz-question",
			QuestionType:       "multi-session",
			Question:           "What happened?",
			QuestionDate:       "2026/01/02 (Fri) 12:00",
			HaystackSessionIDs: []string{"origin-private-session"},
			HaystackDates:      []string{"2025/01/01 (Wed) 10:00"},
			HaystackSessions:   [][]DatasetTurn{turns},
		}
		pairs, err := projectPairs(entry, fixtureProjectionKey, strings.Repeat("a", 64))
		if err != nil {
			t.Fatal(err)
		}
		seen := make(map[string]int)
		for _, pair := range pairs {
			if pair.Prompt != "" {
				seen[pair.Prompt]++
			}
			if pair.Response != "" {
				seen[pair.Response]++
			}
			if _, err := uuid.Parse(pair.PairID); err != nil {
				t.Fatal(err)
			}
			if _, err := uuid.Parse(pair.SessionID); err != nil {
				t.Fatal(err)
			}
			if strings.Contains(pair.PairID, "private") || strings.Contains(pair.SessionID, "origin") {
				t.Fatal("source identity leaked into projected IDs")
			}
		}
		for content := range wantContent {
			if seen[content] != 1 {
				t.Fatalf("content %q appeared %d times", content, seen[content])
			}
		}
	})
}

func FuzzDatasetDigestVerificationRejectsAnyByteMutation(f *testing.F) {
	baseEntries := runtimeDatasetCases()
	baseRaw, err := json.Marshal(baseEntries)
	if err != nil {
		f.Fatal(err)
	}
	f.Add(uint32(0), byte(' '))
	f.Add(uint32(len(baseRaw)/2), byte('\n'))
	f.Add(uint32(len(baseRaw)-1), byte('\t'))
	f.Fuzz(func(t *testing.T, position uint32, replacement byte) {
		if len(baseRaw) == 0 {
			t.Fatal("empty fixture")
		}
		profile := loadTestProfile(t)
		profile.CasesPerCapability = 2
		digest := sha256.Sum256(baseRaw)
		profile.DatasetSHA256 = hex.EncodeToString(digest[:])
		changed := append([]byte(nil), baseRaw...)
		index := int(position % uint32(len(changed)))
		if changed[index] == replacement {
			replacement ^= 0xff
		}
		changed[index] = replacement
		if _, err := LoadSelectedDataset(context.Background(), bytes.NewReader(changed), profile); err == nil {
			t.Fatal("byte-mutated dataset retained pinned identity")
		}
	})
}

func FuzzMonotonicAccountingRejectsCounterRollback(f *testing.F) {
	f.Add(uint64(1), uint8(0))
	f.Add(uint64(100), uint8(6))
	f.Fuzz(func(t *testing.T, base uint64, field uint8) {
		if base == 0 {
			base = 1
		}
		value := ProviderEvidence{
			Lane:              ReaderLane,
			Requests:          base,
			Successes:         base,
			ReceiptedRequests: base,
			PromptTokens:      base,
			CompletionTokens:  base,
			TotalTokens:       base,
			CostUSDmicros:     base,
			ReceiptSetSHA256:  receiptDigestA,
		}
		before := map[string]ProviderEvidence{ReaderLane: value}
		after := cloneSnapshotMap(before)
		changed := after[ReaderLane]
		switch field % 7 {
		case 0:
			changed.Requests--
		case 1:
			changed.Successes--
		case 2:
			changed.ReceiptedRequests--
		case 3:
			changed.PromptTokens--
		case 4:
			changed.CompletionTokens--
		case 5:
			changed.TotalTokens--
		case 6:
			changed.CostUSDmicros--
		}
		after[ReaderLane] = changed
		if err := validateMonotonicSnapshot(before, after); err == nil {
			t.Fatal("counter rollback accepted")
		}
	})
}

func FuzzCanonicalEvidenceRoundTrip(f *testing.F) {
	profileRaw, err := os.ReadFile("testdata/profile-v1.json")
	if err != nil {
		f.Fatal(err)
	}
	var profile Profile
	if err := json.Unmarshal(profileRaw, &profile); err != nil {
		f.Fatal(err)
	}
	selection, err := Select(profile, syntheticCases(profile.CasesPerCapability+5))
	if err != nil {
		f.Fatal(err)
	}
	score, err := Aggregate(selection, balancedOutcomes(selection, 0))
	if err != nil {
		f.Fatal(err)
	}
	evidence, err := NewEvidence(
		profile, selection, artifactDigestA, fixtureLatencyMS, score, providerEvidenceFor(profile),
	)
	if err != nil {
		f.Fatal(err)
	}
	canonical, err := json.Marshal(evidence)
	if err != nil {
		f.Fatal(err)
	}
	f.Add(canonical)
	f.Add([]byte(`{}`))
	f.Add([]byte(`{"score":{"longmem_mean":0}}`))
	f.Fuzz(func(t *testing.T, raw []byte) {
		if len(raw) > 128<<10 {
			t.Skip()
		}
		var decoded Evidence
		if err := json.Unmarshal(raw, &decoded); err != nil {
			return
		}
		if err := decoded.Validate(profile, selection); err != nil {
			return
		}
		firstDigest, err := decoded.Digest(profile, selection)
		if err != nil {
			t.Fatalf("validated evidence could not be digested: %v", err)
		}
		normalized, err := json.Marshal(decoded)
		if err != nil {
			t.Fatal(err)
		}
		var roundTrip Evidence
		if err := json.Unmarshal(normalized, &roundTrip); err != nil {
			t.Fatalf("canonical evidence failed strict decode: %v", err)
		}
		secondDigest, err := roundTrip.Digest(profile, selection)
		if err != nil {
			t.Fatal(err)
		}
		if firstDigest != secondDigest {
			t.Fatalf("canonical round trip moved digest: %s != %s", firstDigest, secondDigest)
		}
	})
}
