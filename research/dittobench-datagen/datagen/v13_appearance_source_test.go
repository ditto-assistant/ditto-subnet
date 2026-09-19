package datagen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
)

func TestV13PrivateAppearanceSelectorResolvesCatalog(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		inv := catalog.InventoryForSeed(seed)
		for kind, options := range map[string][]string{"accent": inv.Accents, "font": inv.Fonts} {
			for targetIndex, target := range options {
				s := v13AppearanceRequestSource(options, target, kind, "")
				anchorIndex := -1
				for i, option := range options {
					if option == s.Values["anchor"] {
						anchorIndex = i
					}
				}
				if anchorIndex < 0 || s.Values["anchor"] == target {
					t.Fatal("missing or target-valued anchor")
				}
				resolved := anchorIndex + 1
				if strings.Contains(s.Kind, "before") {
					resolved = anchorIndex - 1
				}
				if resolved != targetIndex {
					t.Fatal("selector changes desired option")
				}
			}
		}
		for _, pair := range []catalog.NearMiss{inv.AccentNearMiss, inv.FontNearMiss} {
			options := inv.Accents
			if pair == inv.FontNearMiss {
				options = inv.Fonts
			}
			s := v13AppearanceRequestSource(options, pair.Partner, "appearance", pair.Qualifier)
			if s.Values["qualifier"] != pair.Qualifier || !strings.HasSuffix(s.Kind, "_variant") {
				t.Fatal("near-miss distinction lost")
			}
		}
	}
}
