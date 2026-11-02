package persona

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// These full-profile plan hashes come from the parent of PR #2222, before its
// answer-pool correction. The dataset known vectors do not cover this plan path.
func TestPublishedPersonaPlansAcrossSeeds(t *testing.T) {
	vectors := map[int]map[int64]string{
		protocol.BenchVersionV8: {
			3:  "b95bcf60d8a083acc79eca9093b70894f626fa2579e3879081ff36ef055fa5a0",
			41: "969d80efa06896bacbb04ec20cf8fd80ab08b5b9854f01ec58b6c036eed30013",
			99: "0b361947263f107fd212f422dca8fa23915f35383abd175fb306b88f0c2818ab",
		},
		protocol.BenchVersionV9: {
			3:  "82405d8fcee7c3e68a0611153cc522b5a00a00eb854e2885961919cd39d6885f",
			41: "ff7cb671b68e676e364e4462345c4aae719bf823e3397c6cfc819d0b82d6f7c2",
			99: "f569e65216deb24e1bb237eed02fa34ae01aebd1018c5673922ed62d67bf1039",
		},
		protocol.BenchVersionV10: {
			3:  "b82e4c158b9d2bbe04b8006e17cf816002e471b3c495a82e7f33931c926fcc99",
			41: "6ac692a7ab751a66e16d604dfc2da076daa0951583374f6c06a63273871d4317",
			99: "3e305c71ed85609e69d8b87594f1412beb1cffdb87e95040e9e0cb8b7d902e91",
		},
		protocol.BenchVersionV11: {
			3:  "59a62b82677fb97f189a22f6939f0e42f0c4a480f621d18e9e1e44e2371823cb",
			41: "a1257eee80a374085ae41dcd0a223bcda412ed3c17d49064fa4034600854153b",
			99: "5c6028c3e912f600f226d44c6b2c46a984cfcc1684b58e97d58d2a1285872009",
		},
		protocol.BenchVersionV12: {
			3:  "419a8ead8e6aa05aa8f8f8df7e0ae7f87cca60ae32af101dc277f74a330ca189",
			41: "dd33eb2bb0e7b8f360dec393318ac686baae1a3994a4b26d469749e54792f504",
			99: "9affa96fdaa97ba8e7515ca537950ab04cc8901d357c0e20645f05f760ccf124",
		},
		protocol.BenchVersionV13: {
			3:  "95c2b3f08c2dbf91c1789770617859dea0e6720790cc3513a69fc76509034d9c",
			41: "096275ba37706ff9a4b7d876ecf322214c8f6d1ababd1d345cc23b697df4620e",
			99: "a7b4d9c0daf13f28c24819f6838934608d940505016b1a4586014868113f8c30",
		},
	}
	for version, seeds := range vectors {
		for seed, want := range seeds {
			t.Run(fmt.Sprintf("v%d/seed-%d", version, seed), func(t *testing.T) {
				plan, err := BuildPlanForVersion(seed, fullOpts(), version)
				if err != nil {
					t.Fatal(err)
				}
				data, err := json.Marshal(plan)
				if err != nil {
					t.Fatal(err)
				}
				if got := fmt.Sprintf("%x", sha256.Sum256(data)); got != want {
					t.Fatalf("published persona plan changed: got %s, want %s", got, want)
				}
			})
		}
	}
}
