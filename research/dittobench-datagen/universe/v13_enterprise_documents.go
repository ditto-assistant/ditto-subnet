package universe

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"html"
	"strconv"
	"strings"
)

type V13EnterpriseDocument struct{ Body string }

// RenderV13EnterpriseDocuments groups a shuffled, relevance-blind event stream
// into long records. No selector or oracle enters this function. Events from
// different workstreams are equally likely to occupy any record or position.
// eventsPerRecord controls evidence spacing, not the number of repeated facts.
func RenderV13EnterpriseDocuments(w V13EnterpriseWorld, seed int64, format string, eventsPerRecord int) ([]V13EnterpriseDocument, error) {
	if eventsPerRecord < 1 || eventsPerRecord > 256 {
		return nil, fmt.Errorf("enterprise documents: invalid record size")
	}
	switch format {
	case "csv", "json", "markdown", "slack", "email", "transcript":
	default:
		return nil, fmt.Errorf("enterprise documents: unsupported format")
	}
	text, err := RenderV13EnterpriseData(w, seed, "json")
	if err != nil {
		return nil, err
	}
	var events []V13EnterpriseEvent
	if err := json.Unmarshal([]byte(text), &events); err != nil {
		return nil, err
	}
	var out []V13EnterpriseDocument
	for start := 0; start < len(events); start += eventsPerRecord {
		end := start + eventsPerRecord
		if end > len(events) {
			end = len(events)
		}
		group := events[start:end]
		var b strings.Builder
		switch format {
		case "csv", "json":
			// Render the group without revalidating a partial history: its
			// removals/references may be established in a different document.
			if format == "json" {
				raw, err := json.MarshalIndent(group, "", "  ")
				if err != nil {
					return nil, err
				}
				b.Write(raw)
			} else {
				csvText, err := enterpriseCSVGroup(group, seed+int64(start))
				if err != nil {
					return nil, err
				}
				b.WriteString(csvText)
			}
		case "markdown":
			b.WriteString("# Operations log\n\n| Entity | Field | Value | Effective step | Operation |\n| --- | --- | --- | --- | --- |\n")
			for _, e := range group {
				fmt.Fprintf(&b, "| %s | %s | %s | %d | %s |\n", enterpriseMarkdownCell(e.Entity), enterpriseMarkdownCell(e.Field), enterpriseMarkdownCell(e.Value), e.At, e.Operation)
			}
		default:
			if format == "email" {
				b.WriteString("From: operations@fictional.example\nTo: records@fictional.example\nSubject: Consolidated operations notes\n\n")
			}
			if format == "slack" {
				b.WriteString("Pasted Slack-style thread from #operations\n\n")
			}
			if format == "transcript" {
				b.WriteString("Synthetic operations briefing transcript (fictional). Transcript timestamps are playback positions, not effective event times.\n\n")
			}
			for i, e := range group {
				if format == "slack" {
					fmt.Fprintf(&b, "Operations desk — message %d\n", i+1)
				}
				if format == "transcript" {
					fmt.Fprintf(&b, "[%02d:%02d] Operations speaker: ", i/6, (i%6)*10)
				}
				h := sha256.Sum256([]byte(fmt.Sprintf("enterprise-syntax-v1/%d/%s/%s/%d", seed, e.Entity, e.Field, e.At)))
				b.WriteString(enterpriseSentence(e, int(h[0])%3))
				b.WriteString("\n\n")
			}
		}
		if b.Len() > 1<<20 {
			return nil, fmt.Errorf("enterprise documents: record exceeds byte budget")
		}
		out = append(out, V13EnterpriseDocument{Body: b.String()})
	}
	return out, nil
}

func enterpriseMarkdownCell(s string) string {
	s = html.EscapeString(s)
	s = strings.ReplaceAll(s, "|", "&#124;")
	s = strings.ReplaceAll(s, "\r", "&#13;")
	s = strings.ReplaceAll(s, "\n", "&#10;")
	return "<code>" + s + "</code>"
}

func enterpriseSentence(e V13EnterpriseEvent, variant int) string {
	a, f, v := strconv.Quote(e.Entity), strconv.Quote(e.Field), strconv.Quote(e.Value)
	switch e.Operation {
	case "assign":
		switch variant {
		case 0:
			return fmt.Sprintf("Effective at step %d, %s has %s set to %s, replacing any earlier value for that field.", e.At, a, f, v)
		case 1:
			return fmt.Sprintf("For %s, the value of %s becomes %s at effective step %d; prior values of that field are superseded.", a, f, v, e.At)
		default:
			return fmt.Sprintf("The assignment of %s to field %s of %s takes effect at step %d and replaces its previous assignment, if any.", v, f, a, e.At)
		}
	case "add":
		return fmt.Sprintf("At effective step %d, add %s to the %s set of %s, retaining the other members.", e.At, v, f, a)
	case "remove":
		return fmt.Sprintf("At effective step %d, remove %s from the %s set of %s, retaining the other members.", e.At, v, f, a)
	}
	panic("validated enterprise event has unknown operation")
}
