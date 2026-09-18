package gen

import "testing"

func TestV13IsolationLexicalRegression(t *testing.T) {
	profile, _ := ProfileForVersion("full", 13)
	for _, seed := range []int64{2004083, 2004084, 2004085} {
		if _, err := GenerateDataset(seed, profile, 13); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
	}
}
