package parserprobe

import (
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

var asofDateV13 = regexp.MustCompile(`\b(?:January|February|March|April|May|June|July|August|September|October|November|December) [0-9]{1,2}, [0-9]{4}\b`)

func answerAsOfV13(st *store, question string) derived {
	raw := asofDateV13.FindString(question)
	if raw == "" {
		return derived{}
	}
	anchor, err := time.Parse("January 2, 2006", raw)
	if err != nil {
		return derived{}
	}
	anchor = anchor.Add(12 * time.Hour)
	past := newStore(st.version)
	for _, p := range st.pairs {
		at, err := time.Parse(time.RFC3339, p.Timestamp)
		// Identity/context can be recorded after the requested historical
		// instant. Retain those joins while time-filtering mutable facts.
		identity := strings.Contains(p.Prompt, "“") || containsPhraseFuzzy(p.Prompt, "these days") || containsPhraseFuzzy(p.Prompt, "the one through")
		if identity || (err == nil && !at.After(anchor)) {
			past.ingest(p)
		}
	}
	past.finish()
	if containsFuzzy(question, "email") {
		for _, p := range past.people {
			if !strings.Contains(question, p.name) {
				continue
			}
			value := p.email
			if value == "" {
				value = p.previousEmail
			}
			if value != "" {
				return derived{family: "point-in-time-contact", kind: protocol.AnswerValue, value: value, ok: true}
			}
		}
	}
	if containsFuzzy(question, "invoice") {
		for _, p := range past.projects {
			if p.alias == "" || !strings.Contains(question, p.alias) || !p.hasOriginal {
				continue
			}
			value := p.originalCents
			if p.hasCorrection {
				value = p.correctedCents
			}
			return derived{family: "point-in-time-invoice", kind: protocol.AnswerMoney, value: fmt.Sprint(value), ok: true}
		}
	}
	if containsFuzzy(question, "days") {
		for _, trip := range past.trips {
			if trip.alias == "" || !strings.Contains(question, trip.alias) || !trip.hasPlan {
				continue
			}
			for i, country := range trip.countries {
				if !strings.Contains(question, country) {
					continue
				}
				days := trip.oldLegs[i]
				if trip.hasCorrection && strings.EqualFold(country, trip.deltaCountry) {
					days = maxInt(days+trip.deltaDays, 2)
				}
				return derived{family: "point-in-time-trip-leg", kind: protocol.AnswerNumber, value: fmt.Sprint(days), ok: true}
			}
		}
	}
	return derived{}
}
