package universe

import (
	"encoding/csv"
	"encoding/json"
	"html"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

func TestV13EnterpriseDocumentRoundtrip(t *testing.T) {
	w, _ := GenerateV13Enterprise(44, 12, 9)
	w.Events = append(w.Events, V13EnterpriseEvent{w.Entities[0], "note", "ID 00123 | quoted \"text\"\n<code> & newline", 0, "assign"})
	for _, format := range []string{"csv", "json", "markdown"} {
		docs, err := RenderV13EnterpriseDocuments(w, 555, format, 37)
		if err != nil {
			t.Fatal(err)
		}
		if len(docs) < 3 {
			t.Fatal("not a multi-record corpus")
		}
		var events []V13EnterpriseEvent
		for _, doc := range docs {
			if format == "json" {
				var group []V13EnterpriseEvent
				if err := json.Unmarshal([]byte(doc.Body), &group); err != nil {
					t.Fatal(err)
				}
				events = append(events, group...)
				continue
			}
			var rows [][]string
			if format == "csv" {
				rows, err = csv.NewReader(strings.NewReader(doc.Body)).ReadAll()
				if err != nil {
					t.Fatal(err)
				}
			} else {
				rows = [][]string{{"entity", "field", "value", "effective_step", "operation"}}
				for _, line := range strings.Split(doc.Body, "\n")[4:] {
					if line == "" {
						continue
					}
					cells := strings.Split(line, "|")[1:6]
					for i, c := range cells {
						c = strings.TrimSpace(c)
						c = strings.TrimSuffix(strings.TrimPrefix(c, "<code>"), "</code>")
						cells[i] = html.UnescapeString(c)
					}
					rows = append(rows, cells)
				}
			}
			for _, row := range rows[1:] {
				m := map[string]string{}
				for i, k := range rows[0] {
					m[k] = row[i]
				}
				at, err := strconv.Atoi(m["effective_step"])
				if err != nil {
					t.Fatal(err)
				}
				events = append(events, V13EnterpriseEvent{m["entity"], m["field"], m["value"], at, m["operation"]})
			}
		}
		before, _ := w.state(99)
		after, err := (V13EnterpriseWorld{w.Entities, events}).state(99)
		if err != nil || len(events) != len(w.Events) || !reflect.DeepEqual(before, after) {
			t.Fatalf("lossy %s: %v", format, err)
		}
	}
}

func TestV13EnterpriseNarrativeFrames(t *testing.T) {
	w, _ := GenerateV13Enterprise(99, 12, 12)
	for _, format := range []string{"slack", "email", "transcript"} {
		docs, err := RenderV13EnterpriseDocuments(w, 91, format, 128)
		if err != nil {
			t.Fatal(err)
		}
		again, err := RenderV13EnterpriseDocuments(w, 91, format, 128)
		if err != nil || !reflect.DeepEqual(docs, again) {
			t.Fatal("not reproducible")
		}
		var joined strings.Builder
		for _, doc := range docs {
			joined.WriteString(doc.Body)
		}
		if len(docs[0].Body) < 10000 {
			t.Fatal("long-record coverage missing")
		}
		for _, e := range w.Events {
			found := false
			for v := 0; v < 3; v++ {
				found = found || strings.Contains(joined.String(), enterpriseSentence(e, v))
			}
			if !found {
				t.Fatal("lost fact")
			}
		}
	}
	if _, err := RenderV13EnterpriseDocuments(w, 1, "docx", 8); err == nil {
		t.Fatal("unsupported format accepted")
	}
	if _, err := RenderV13EnterpriseDocuments(w, 1, "email", 0); err == nil {
		t.Fatal("invalid record bound accepted")
	}
}
