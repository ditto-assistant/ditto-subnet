// Command graderaudit builds the grader false-negative labeling sheet (prod
// hardening P3, in the spirit of the SWE-bench Verified / WebArena Verified
// grader audits).
//
// Deterministic grading's weak spot is a correct answer phrased outside the
// normalization tables. This tool finds the cases worth a human look: every
// memory case that survived the disqualifying scans (forbidden value,
// distractor, wrong abstention) but scored 0 at the typed answer check. Those
// are the only place a grader false negative can hide; a zero from a
// disqualifier is the adversarial machinery working as designed and is
// excluded from the sheet.
//
// Usage:
//
//	graderaudit -artifact dataset.json -transcripts transcripts.jsonl [-json]
//
// The artifact is the canonical DatasetArtifact JSON (generate -out). The
// transcript file is JSONL, one line per case:
//
//	{"case_id": "c1a2b3c4d5e6f7a8", "response": {<protocol.RunResponse>}}
//
// Output: a labeling sheet on stdout (TSV by default, JSONL with -json) with
// one row per candidate false negative: case id, question type, answer kind,
// question, expected answer, list items, and the harness response. A summary
// of per-answer-kind counts (graded, credited, disqualified, typed zeroes)
// goes to stderr, so repeated audits can publish the measured rate per kind.
//
// The sheet is for HUMAN labeling: a row is a real false negative only when a
// human reads the response as correct. Measured rates feed the public docs
// per bench version; fixes land as normalization-table changes in the grade
// package, which alter grading (not dataset bytes) and are re-runnable over
// recorded transcripts.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// transcriptLine is one JSONL record tying a case to the harness response it
// received.
type transcriptLine struct {
	CaseID   string               `json:"case_id"`
	Response protocol.RunResponse `json:"response"`
}

// outcome classifies one graded case for the audit.
type outcome string

const (
	outCredited     outcome = "credited"     // score > 0
	outDisqualified outcome = "disqualified" // forbidden/distractor/wrong-abstain zero
	outTypedZero    outcome = "typed_zero"   // survived scans, failed the typed check
	outEmpty        outcome = "empty"        // empty response
	outMissing      outcome = "missing"      // no transcript for the case
)

// candidate is one labeling-sheet row.
type candidate struct {
	CaseID       string   `json:"case_id"`
	QuestionType string   `json:"question_type"`
	AnswerKind   string   `json:"answer_kind"`
	Question     string   `json:"question"`
	Expected     string   `json:"expected_answer"`
	Items        []string `json:"answer_items,omitempty"`
	Answer       string   `json:"answer,omitempty"`
	FinalText    string   `json:"final_text"`
	Notes        []string `json:"notes,omitempty"`
}

// kindStat is the per-answer-kind tally for the stderr summary.
type kindStat struct {
	Graded       int
	Credited     int
	Disqualified int
	TypedZero    int
}

// classify grades one case and buckets the verdict. It keys the disqualifier
// bucket on the grader's own note strings, which is safe here because tool
// and grader live in one module and change together.
func classify(mc protocol.MemoryCase, resp protocol.RunResponse) (outcome, grade.Verdict) {
	v := grade.Memory(mc, resp)
	if v.Score > 0 {
		return outCredited, v
	}
	if len(v.Notes) == 0 {
		return outTypedZero, v
	}
	n := v.Notes[0]
	switch {
	case n == "empty response":
		return outEmpty, v
	case strings.HasPrefix(n, "no deterministic "):
		return outTypedZero, v
	default:
		return outDisqualified, v
	}
}

// audit runs the classification over every memory case with a transcript and
// returns the labeling sheet plus per-kind stats.
func audit(cases []gen.ArtifactCase, transcripts map[string]protocol.RunResponse) ([]candidate, map[string]*kindStat, int) {
	stats := map[string]*kindStat{}
	statFor := func(kind string) *kindStat {
		if kind == "" {
			kind = protocol.AnswerValue
		}
		if stats[kind] == nil {
			stats[kind] = &kindStat{}
		}
		return stats[kind]
	}
	var sheet []candidate
	missing := 0
	for _, ac := range cases {
		resp, ok := transcripts[ac.ID]
		if !ok {
			missing++
			continue
		}
		st := statFor(ac.AnswerKind)
		st.Graded++
		out, v := classify(ac.MemoryCase, resp)
		switch out {
		case outCredited:
			st.Credited++
		case outDisqualified:
			st.Disqualified++
		case outTypedZero, outEmpty:
			st.TypedZero++
			sheet = append(sheet, candidate{
				CaseID:       ac.ID,
				QuestionType: ac.QuestionType,
				AnswerKind:   kindOrValue(ac.AnswerKind),
				Question:     ac.Question,
				Expected:     ac.ExpectedAnswer,
				Items:        ac.AnswerItems,
				Answer:       resp.Answer,
				FinalText:    resp.FinalText,
				Notes:        v.Notes,
			})
		}
	}
	return sheet, stats, missing
}

func kindOrValue(k string) string {
	if k == "" {
		return protocol.AnswerValue
	}
	return k
}

func main() {
	artifactPath := flag.String("artifact", "", "path to the canonical DatasetArtifact JSON")
	transcriptPath := flag.String("transcripts", "", "path to JSONL transcripts ({case_id, response} per line)")
	asJSON := flag.Bool("json", false, "emit the labeling sheet as JSONL instead of TSV")
	flag.Parse()
	if *artifactPath == "" || *transcriptPath == "" {
		fmt.Fprintln(os.Stderr, "usage: graderaudit -artifact dataset.json -transcripts transcripts.jsonl [-json]")
		os.Exit(2)
	}

	ab, err := os.ReadFile(*artifactPath)
	fatal(err, "read artifact")
	var artifact gen.DatasetArtifact
	fatal(json.Unmarshal(ab, &artifact), "parse artifact")

	tf, err := os.Open(*transcriptPath)
	fatal(err, "open transcripts")
	defer tf.Close()
	transcripts := map[string]protocol.RunResponse{}
	sc := bufio.NewScanner(tf)
	sc.Buffer(make([]byte, 0, 1<<20), 1<<24)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var tl transcriptLine
		fatal(json.Unmarshal([]byte(line), &tl), "parse transcript line")
		transcripts[tl.CaseID] = tl.Response
	}
	fatal(sc.Err(), "scan transcripts")

	sheet, stats, missing := audit(artifact.MemoryCases, transcripts)

	w := bufio.NewWriter(os.Stdout)
	defer w.Flush()
	if *asJSON {
		enc := json.NewEncoder(w)
		for _, c := range sheet {
			fatal(enc.Encode(c), "encode row")
		}
	} else {
		fmt.Fprintln(w, "case_id\tquestion_type\tanswer_kind\tquestion\texpected\tresponse")
		for _, c := range sheet {
			fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\n",
				c.CaseID, c.QuestionType, c.AnswerKind,
				tsv(c.Question), tsv(c.Expected+joinItems(c.Items)), tsv(c.Answer+" | "+c.FinalText))
		}
	}

	kinds := make([]string, 0, len(stats))
	for k := range stats {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)
	fmt.Fprintf(os.Stderr, "graderaudit: %d candidate false negatives across %d graded cases (%d without transcripts)\n",
		len(sheet), totalGraded(stats), missing)
	for _, k := range kinds {
		s := stats[k]
		fmt.Fprintf(os.Stderr, "  %-14s graded=%-4d credited=%-4d disqualified=%-4d typed_zero=%d\n",
			k, s.Graded, s.Credited, s.Disqualified, s.TypedZero)
	}
}

func totalGraded(stats map[string]*kindStat) int {
	n := 0
	for _, s := range stats {
		n += s.Graded
	}
	return n
}

func joinItems(items []string) string {
	if len(items) == 0 {
		return ""
	}
	return " [" + strings.Join(items, "; ") + "]"
}

// tsv flattens a field for the TSV sheet.
func tsv(s string) string {
	s = strings.ReplaceAll(s, "\t", " ")
	return strings.ReplaceAll(s, "\n", " ")
}

func fatal(err error, stage string) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "graderaudit: %s: %v\n", stage, err)
		os.Exit(1)
	}
}
