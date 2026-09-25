package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestLifecycleWriteCategoryUnreachableFromV8 backs the scorer's v13
// memory-write exclusion (services/dittobench-api scorer.memoryWriteCategory):
// the category the over-call factor used to exclude is not generated from v8 on,
// so that exclusion protected nothing, while the live declarative-acknowledgement
// category — whose intended work is a memory write — is generated at every
// version the scorer change reasons about.
func TestLifecycleWriteCategoryUnreachableFromV8(t *testing.T) {
	for _, version := range []int{protocol.BenchVersionV8, protocol.BenchVersionV12, protocol.BenchVersionV13} {
		prof, ok := ProfileForVersion("full", version)
		if !ok {
			t.Fatalf("v%d has no full profile", version)
		}
		artifact, err := GenerateDataset(123456789, prof, version)
		if err != nil {
			t.Fatalf("v%d: %v", version, err)
		}
		ack := 0
		for _, c := range artifact.MemoryCases {
			if c.QuestionType == QTLifecycleWrite {
				t.Fatalf("v%d generated a %s case; the scorer's exclusion assumes it cannot", version, QTLifecycleWrite)
			}
			if c.QuestionType == QTDeclarativeAck {
				ack++
			}
		}
		if ack == 0 {
			t.Fatalf("v%d generated no %s case, so the live write-shaped category vanished", version, QTDeclarativeAck)
		}
	}
}
