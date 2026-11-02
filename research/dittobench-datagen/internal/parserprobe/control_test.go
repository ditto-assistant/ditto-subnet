package parserprobe

import (
	"fmt"
	"math"
	"testing"
)

func controlFixture() Report {
	return controlFixtureForVersion(13)
}

func controlFixtureForVersion(version int) Report {
	r := Report{BenchVersion: version, RunSize: "full", Source: "generated", GIH: newVariant()}
	for i := range 40 {
		r.Seeds = append(r.Seeds, SeedReport{Seed: int64(i), BenchVersion: version})
	}
	for _, slice := range []string{"story", "programs", "personal", "quantity", "tool-prompts"} {
		r.GIH.Slices[slice] = Stats{Cases: 40, Mean: 0.90}
	}
	return r
}

func TestPublicControlFailures(t *testing.T) {
	if failures := PublicControlFailures(controlFixture()); len(failures) != 0 {
		t.Fatal(failures)
	}
	if len(PublicControlFailures(controlFixtureForVersion(14))) == 0 {
		t.Fatal("legacy v13 checker accepted v14 evidence")
	}
	for _, version := range []int{13, 14} {
		t.Run(fmt.Sprintf("v%d", version), func(t *testing.T) {
			testPublicControlFailuresForVersion(t, version)
		})
	}
}

func testPublicControlFailuresForVersion(t *testing.T, version int) {
	t.Helper()
	if failures := PublicControlFailuresForVersion(controlFixtureForVersion(version), version); len(failures) != 0 {
		t.Fatal(failures)
	}
	for name, mutate := range map[string]func(*Report){
		"private artifact": func(r *Report) { r.Source = "artifacts" },
		"wrong version":    func(r *Report) { r.BenchVersion = 12 },
		"small profile":    func(r *Report) { r.RunSize = "small" },
		"too few seeds":    func(r *Report) { r.Seeds = r.Seeds[:39] },
		"duplicate seed":   func(r *Report) { r.Seeds[39].Seed = r.Seeds[0].Seed },
		"mixed version":    func(r *Report) { r.Seeds[39].BenchVersion = 12 },
		"unclassified":     func(r *Report) { r.Unclassified = []string{"unknown"} },
		"missing GIH":      func(r *Report) { r.GIH = nil },
		"missing slice":    func(r *Report) { delete(r.GIH.Slices, "quantity") },
		"weak control":     func(r *Report) { r.GIH.Slices["tool-prompts"] = Stats{Cases: 4000, Mean: 0.237} },
		"empty coverage":   func(r *Report) { r.GIH.Slices["story"] = Stats{Mean: 1} },
		"nan":              func(r *Report) { r.GIH.Slices["story"] = Stats{Cases: 40, Mean: math.NaN()} },
		"infinite":         func(r *Report) { r.GIH.Slices["story"] = Stats{Cases: 40, Mean: math.Inf(1)} },
		"invalid score":    func(r *Report) { r.GIH.Slices["story"] = Stats{Cases: 40, Mean: 1.1} },
	} {
		t.Run(name, func(t *testing.T) {
			r := controlFixtureForVersion(version)
			mutate(&r)
			if len(PublicControlFailuresForVersion(r, version)) == 0 {
				t.Fatal("invalid control passed")
			}
		})
	}
}

func TestPublicControlVersionBinding(t *testing.T) {
	for _, target := range []int{13, 14} {
		other := 13
		if target == 13 {
			other = 14
		}
		if len(PublicControlFailuresForVersion(controlFixtureForVersion(other), target)) == 0 {
			t.Fatalf("v%d control accepted a v%d report", target, other)
		}
		r := controlFixtureForVersion(target)
		r.Seeds[0].BenchVersion = other
		if len(PublicControlFailuresForVersion(r, target)) == 0 {
			t.Fatalf("v%d control accepted a v%d seed", target, other)
		}
	}
	for _, version := range []int{12, 15} {
		if len(PublicControlFailuresForVersion(controlFixtureForVersion(version), version)) == 0 {
			t.Fatalf("unreviewed v%d control requirements were inherited", version)
		}
	}
}
