package parserprobe

import (
	"math"
	"testing"
)

func controlFixture() Report {
	r := Report{BenchVersion: 13, RunSize: "full", Source: "generated", GIH: newVariant()}
	for i := range 40 {
		r.Seeds = append(r.Seeds, SeedReport{Seed: int64(i), BenchVersion: 13})
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
			r := controlFixture()
			mutate(&r)
			if len(PublicControlFailures(r)) == 0 {
				t.Fatal("invalid control passed")
			}
		})
	}
}
