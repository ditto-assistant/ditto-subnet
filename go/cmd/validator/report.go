package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// minerReport is the on-disk payload written per miner when --report-dir
// is set. The shape mirrors ditto/bench/runner/report.py so existing
// dashboards keep working when they ingest validator output instead of
// the Python contributor runner output.
type minerReport struct {
	SchemaVersion string             `json:"schema_version"`
	Hotkey        string             `json:"hotkey"`
	Image         string             `json:"image"`
	GeneratedAt   time.Time          `json:"generated_at"`
	Scores        []bittensor.Score  `json:"scores"`
	Aggregates    []mechanismSummary `json:"aggregates"`
}

type mechanismSummary struct {
	Mechanism string  `json:"mechanism"`
	Count     int     `json:"count"`
	MeanScore float64 `json:"mean_score"`
}

func writeMinerReport(path, hotkey string, commit MinerCommitment, scores []scoreWithMech) error {
	flat := make([]bittensor.Score, len(scores))
	for i, s := range scores {
		flat[i] = s.Score
	}
	rep := minerReport{
		SchemaVersion: bittensor.SchemaVersion,
		Hotkey:        hotkey,
		Image:         commit.Image,
		GeneratedAt:   time.Now().UTC(),
		Scores:        flat,
		Aggregates:    summariseByMechanism(scores),
	}
	buf, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(buf, '\n'), 0o644)
}

func summariseByMechanism(scores []scoreWithMech) []mechanismSummary {
	type bucket struct {
		sum   float64
		count int
	}
	buckets := map[bittensor.Mechanism]*bucket{}
	for _, s := range scores {
		b, ok := buckets[s.Mechanism]
		if !ok {
			b = &bucket{}
			buckets[s.Mechanism] = b
		}
		b.sum += s.Score.Score
		b.count++
	}
	out := make([]mechanismSummary, 0, len(buckets))
	for mech, b := range buckets {
		mean := 0.0
		if b.count > 0 {
			mean = b.sum / float64(b.count)
		}
		out = append(out, mechanismSummary{
			Mechanism: string(mech),
			Count:     b.count,
			MeanScore: mean,
		})
	}
	return out
}

// sanitize keeps file paths reasonable when the hotkey contains chars
// the local filesystem would reject (it usually doesn't, but defensive).
func sanitize(s string) string {
	repl := func(r rune) rune {
		switch {
		case r >= 'A' && r <= 'Z':
			return r
		case r >= 'a' && r <= 'z':
			return r
		case r >= '0' && r <= '9':
			return r
		case r == '-' || r == '_':
			return r
		default:
			return '_'
		}
	}
	return strings.Map(repl, s)
}

// echoSink writes a small JSON summary to stdout so `make
// validator-smoke` can grep for it without parsing a full report.
func echoSink(w *os.File, aggregates []mechanismSummary) {
	for _, agg := range aggregates {
		fmt.Fprintf(w, "%s: n=%d mean=%.3f\n", agg.Mechanism, agg.Count, agg.MeanScore)
	}
}
