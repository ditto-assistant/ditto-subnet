package parserprobe

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestEnterpriseV13WireParser(t *testing.T) {
	formats := map[string]bool{}
	for seed := int64(0); seed < 12; seed++ {
		cases, err := universe.GenerateV13EnterprisePrograms(seed, 12)
		if err != nil {
			t.Fatal(err)
		}
		st := newStore(13)
		for _, c := range cases {
			st.pairs = append(st.pairs, c.Pairs...)
		}
		for _, c := range cases {
			d := answerEnterpriseV13(st, c.Plan.Case.Question)
			if !d.ok || d.value != c.Plan.Case.ExpectedAnswer {
				t.Fatalf("seed %d format %s: got %+v want %s", seed, c.Provenance.Renderer, d, c.Plan.Case.ExpectedAnswer)
			}
			formats[string(c.Provenance.Renderer)] = true
			if missing := answerEnterpriseV13(newStore(13), c.Plan.Case.Question); missing.ok {
				t.Fatal("answered without served evidence")
			}
		}
	}
	if len(formats) != 6 {
		t.Fatalf("format coverage: %v", formats)
	}
}
