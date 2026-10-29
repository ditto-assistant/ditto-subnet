package main

import (
	"net/url"
	"os"
	"regexp"
	"runtime/debug"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
)

// The scorer advertises what it can run on every work poll so Platform can
// refuse work (and generator settings) outside this build's pack contract
// instead of leasing a request the build would only report as a fault. On
// 2026-10-27 a stale build (MaxCases 64) was leased a 70-case round.

var (
	scorerIDPattern = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,120}$`)
	revisionPattern = regexp.MustCompile(`^[0-9a-f]{7,64}$`)
)

// defaultScorerID names this host when -scorer-id is unset.
func defaultScorerID() string {
	if host, err := os.Hostname(); err == nil && scorerIDPattern.MatchString(host) {
		return host
	}
	return "default"
}

// buildRevision is the commit the binary was built from, when known: the
// release stamp (-X main.releaseRevision), else Go's VCS build info.
func buildRevision() string {
	if revisionPattern.MatchString(releaseRevision) {
		return releaseRevision
	}
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return ""
	}
	for _, s := range info.Settings {
		if s.Key == "vcs.revision" && revisionPattern.MatchString(s.Value) {
			return s.Value
		}
	}
	return ""
}

// advertisement is the query a work poll carries: Platform's
// generator_scorer_capability reads it.
func advertisement(scorerID string, sb genrun.Sandbox) url.Values {
	q := url.Values{}
	if !scorerIDPattern.MatchString(scorerID) {
		scorerID = "default"
	}
	q.Set("scorer_id", scorerID)
	q.Set("scorer_version", ScorerVersion)
	q.Set("min_cases", strconv.Itoa(challenge.MinCases))
	q.Set("max_cases", strconv.Itoa(challenge.MaxCases))
	// The pack grading policies this build requests and grades, explicitly,
	// the way validators advertise generator-round-bench-<n>: Platform opens
	// and leases a round at N (and requests a qualification at N) only to a
	// scorer listing N. A scorer that predates this list is treated as
	// v13-only.
	q.Set("grading_bench_versions", gradingVersionsParam(challenge.SupportedGradingBenchVersions()))
	if sb != nil {
		q.Set("sandbox", sb.Name())
	}
	if rev := buildRevision(); rev != "" {
		q.Set("build_revision", rev)
	}
	return q
}

// gradingVersionsParam encodes the policy list as "13,14".
func gradingVersionsParam(versions []int) string {
	parts := make([]string, len(versions))
	for i, v := range versions {
		parts[i] = strconv.Itoa(v)
	}
	return strings.Join(parts, ",")
}

// pollPath appends the advertisement to a work-poll path.
func (c *platformClient) pollPath(path string) string {
	if len(c.advertise) == 0 {
		return path
	}
	return path + "?" + c.advertise.Encode()
}
