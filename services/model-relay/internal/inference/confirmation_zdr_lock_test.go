package inference

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"

	"github.com/ditto-assistant/model-relay/internal/postgres"
)

// scorerJudgeProviderGolden is written by a dittobench-api test from
// judgeProviderRouting itself: the scorer's exact judge provider block.
const scorerJudgeProviderGolden = "../../../dittobench-api/internal/longmemeval/testdata/judge-provider-routing-v1.json"

func decodeGoldenJSON(t *testing.T, raw []byte) any {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func zdrTestPayload(t *testing.T, provider any) map[string]any {
	t.Helper()
	return map[string]any{
		"model":                 "deepseek/deepseek-v4.1-flash",
		"messages":              []any{map[string]any{"role": "user", "content": "Is the answer correct?"}},
		"max_completion_tokens": json.Number("64"),
		"provider":              provider,
	}
}

func togetherJudgeGrant() *postgres.ConfirmationInferenceGrant {
	return &postgres.ConfirmationInferenceGrant{
		Lane: "judge", Model: "deepseek/deepseek-v4.1-flash", RouteProvider: "together",
	}
}

// TestScorerJudgeProviderBlockPassesTheProductionLock feeds the scorer's real
// judge block through THIS lock -- the one that serves /api/v1/inference/* in
// prod. #3167 added zdr to that block, this lock compared it for exact equality
// with a pin lacking zdr, and every judge request was 403'd, failing LongMem
// confirmation on every bundle from v0.397.0 (ditto-subnet#3214). The test
// that existed compared the block with a Python mirror, not with the receiver.
func TestScorerJudgeProviderBlockPassesTheProductionLock(t *testing.T) {
	raw, err := os.ReadFile(scorerJudgeProviderGolden)
	if err != nil {
		t.Fatalf("read scorer golden %s: %v (the monorepo checkout is required)", scorerJudgeProviderGolden, err)
	}
	block := decodeGoldenJSON(t, raw)
	if fields, _ := block.(map[string]any); fields["zdr"] != true {
		t.Fatalf("golden no longer exercises the zdr case: %s", raw)
	}

	upstream, _, herr := lockedConfirmationChatPayload(zdrTestPayload(t, block), togetherJudgeGrant(), 128)

	if herr != nil {
		t.Fatalf("production lock refused the scorer's judge block: %v", herr)
	}
	pinned, _ := upstream["provider"].(map[string]any)
	if pinned["zdr"] != true {
		t.Fatalf("upstream pin lost zdr: %v", pinned)
	}
}

func TestConfirmationLockAcceptsZDRAbsentOrTrueAndAlwaysForcesIt(t *testing.T) {
	for _, lane := range []string{"judge", "reader"} {
		route := "together"
		if lane == "reader" {
			route = confirmationReaderRouteProvider
		}
		grant := &postgres.ConfirmationInferenceGrant{Lane: lane, Model: "deepseek/deepseek-v4.1-flash", RouteProvider: route}
		absent := confirmationChatProviderPreferences(lane, route)
		present := confirmationChatProviderPreferences(lane, route)
		present["zdr"] = true
		for name, provider := range map[string]map[string]any{"absent": absent, "true": present} {
			upstream, _, herr := lockedConfirmationChatPayload(zdrTestPayload(t, provider), grant, 128)
			if herr != nil {
				t.Fatalf("%s lane, zdr %s: refused: %v", lane, name, herr)
			}
			if pinned, _ := upstream["provider"].(map[string]any); pinned["zdr"] != true {
				t.Fatalf("%s lane, zdr %s: upstream lost zdr: %v", lane, name, pinned)
			}
		}
	}
}

func TestConfirmationLockRefusesAnyZDROtherThanTrue(t *testing.T) {
	for _, zdr := range []any{false, nil, "true", json.Number("1")} {
		provider := confirmationChatProviderPreferences("judge", "together")
		provider["zdr"] = zdr
		_, _, herr := lockedConfirmationChatPayload(zdrTestPayload(t, provider), togetherJudgeGrant(), 128)
		if herr == nil || herr.status != 403 {
			t.Fatalf("zdr=%#v accepted (err=%v); an opt-out must not be silently overridden", zdr, herr)
		}
	}
}

func TestConfirmationLockStillRefusesAnExtraKeyBesideZDR(t *testing.T) {
	provider := confirmationChatProviderPreferences("judge", "together")
	provider["zdr"] = true
	provider["sort"] = "price"
	if _, _, herr := lockedConfirmationChatPayload(zdrTestPayload(t, provider), togetherJudgeGrant(), 128); herr == nil {
		t.Fatal("tolerating zdr loosened the lock for another key")
	}
}
