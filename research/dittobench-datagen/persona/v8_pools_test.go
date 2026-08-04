package persona

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV8AmbiguousAnswerPoolsAreQualified(t *testing.T) {
	for attr, legacy := range map[string][]string{
		"favorite_color":   colors,
		"primary_language": softwareLanguages,
	} {
		pool := answerPoolForVersion(attr, legacy, protocol.BenchVersionV8)
		for _, value := range pool {
			parts := strings.Fields(value)
			if len(parts) < 2 {
				t.Fatalf("v8 %s value %q lacks a disambiguating qualifier", attr, value)
			}
			for _, part := range parts {
				if grade.Hit(value, "Let me "+strings.ToLower(part)+" check that.") {
					t.Fatalf("v8 %s value %q is creditable from incidental token %q", attr, value, part)
				}
			}
		}
	}
	if !grade.Hit("Go", "Let me go check that.") {
		t.Fatal("test fixture no longer reproduces the frozen v7 Go collision")
	}
	if grade.Hit(answerPoolForVersion("primary_language", softwareLanguages, protocol.BenchVersionV8)[1], "Let me go check that.") {
		t.Fatal("v8 qualified Go value matched incidental prose")
	}
	if got := answerPoolForVersion("primary_language", softwareLanguages, protocol.BenchVersionV7); &got[0] != &softwareLanguages[0] {
		t.Fatal("v7 software-language pool changed")
	}
}
