package persona

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestEveryV8AnswerPoolRejectsIncidentalTokens(t *testing.T) {
	pools := map[string][]string{
		"city": cities, "occupation": occupations, "employer": companies,
		"car": carModels, "partner": firstNames, "instrument": instruments,
		"alma_mater": universities, "project": projectNames, "pet": petNames,
		"favorite_cuisine": cuisines, "dietary": dietaryStyles,
		"favorite_color": colors, "primary_language": softwareLanguages,
		"code_editor": softwareEditors, "service": softwareServices,
		"diagnosis": medicalDiagnoses, "medication": medicalMedications,
		"allergy": medicalAllergies, "practice_area": legalPracticeAreas,
		"bar_admission": legalJurisdictions, "legal_matter": legalMatters,
		"risk_tolerance": financeRiskProfiles, "brokerage": financeBrokerages,
		"holding": financeHoldings, "shoe_size": shoeSizes, "height": heightsCm,
		"favorite_song": favoriteSongs, "star_sign": starSigns,
		"middle_name": firstNames, "eye_color": eyeColors,
		"blood_type": bloodTypes, "birthday_month": birthdayMonths,
		"sports_team": sportsTeams, "favorite_film": favoriteFilms,
	}
	for attr, legacy := range pools {
		pool := answerPoolForVersion(attr, legacy, protocol.BenchVersionV8)
		for _, value := range pool {
			for _, sentence := range v8IncidentalProse {
				if grade.Hit(value, sentence) {
					t.Errorf("v8 %s value %q hits incidental %q", attr, value, sentence)
				}
			}
		}
	}
}

func TestV8IncidentalExposureIsZeroAcrossOneHundredSeeds(t *testing.T) {
	v8Hits := 0
	v9Hits := 0
	v7Hits := 0
	for seed := int64(1); seed <= 100; seed++ {
		v8, err := BuildPlanForVersion(seed, fullOpts(), protocol.BenchVersionV8)
		if err != nil {
			t.Fatal(err)
		}
		v9, err := BuildPlanForVersion(seed, fullOpts(), protocol.BenchVersionV9)
		if err != nil {
			t.Fatal(err)
		}
		v7, err := BuildPlanForVersion(seed, fullOpts(), protocol.BenchVersionV7)
		if err != nil {
			t.Fatal(err)
		}
		v8Hits += incidentalHits(t, "v8", v8)
		v9Hits += incidentalHits(t, "v9", v9)
		v7Hits += incidentalHits(t, "v7", v7)
	}
	t.Logf("incidental grade.Hit collisions over 100 full-profile seeds: v7=%d v8=%d v9=%d", v7Hits, v8Hits, v9Hits)
	if v8Hits != 0 || v9Hits != 0 {
		t.Fatalf("qualified plans still contain incidental answer collisions: v8=%d v9=%d", v8Hits, v9Hits)
	}
	if v7Hits == 0 {
		t.Fatal("v7 fixture no longer reproduces the frozen collisions")
	}
}

func incidentalHits(t *testing.T, label string, plan *Plan) int {
	t.Helper()
	hits := 0
	for _, fact := range plan.Facts {
		for _, sentence := range v8IncidentalProse {
			if grade.Hit(fact.Value, sentence) {
				if label != "v7" && hits < 20 {
					t.Logf("%s seed fact %s=%q hits %q", label, fact.Attribute, fact.Value, sentence)
				}
				hits++
				break
			}
		}
	}
	return hits
}

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
