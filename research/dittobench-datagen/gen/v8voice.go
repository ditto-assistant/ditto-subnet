package gen

import (
	"github.com/ditto-assistant/dittobench-datagen/internal/assistantvoice"
	"github.com/ditto-assistant/dittobench-datagen/internal/uservoice"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func applyV8AssistantVoice(seed int64, userName string, waves []protocol.SeedRequest) {
	for i := range waves {
		for j := range waves[i].Pairs {
			pair := &waves[i].Pairs[j]
			pair.Prompt = uservoice.Render(pair.Prompt)
			pair.Response = assistantvoice.Render(seed, pair.PairID, pair.SessionID, userName, pair.Prompt, pair.Response)
		}
	}
}
