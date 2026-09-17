package parserprobe

import (
	"regexp"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

var personIdentityV13 = []businessFrame{
	businessPattern("%s is my %s. Everyone there calls them “%s.”", "name", "relation", "nick"),
	businessPattern("My %s, %s, goes by “%s” with everyone there.", "relation", "name", "nick"),
	businessPattern("“%s” is what everyone calls %s, my %s.", "nick", "name", "relation"),
	businessPattern("%s — my %s — is “%s” to everyone who knows them.", "name", "relation", "nick"),
	businessPattern("you'll hear %s called “%s” by everyone; they're my %s.", "name", "nick", "relation"),
	businessPattern("for the record, %s is my %s, and the whole crowd calls them “%s.”", "name", "relation", "nick"),
}
var projectIdentityV13 = []businessFrame{
	businessPattern("When I say “%s” I mean %s for %s, not the similarly named client work.", "alias", "name", "client"),
	businessPattern("“%s” is just my shorthand for %s, the %s engagement — don't confuse it with the lookalike client job.", "alias", "name", "client"),
	businessPattern("%s and “%s” are the same project, the one for %s; the similarly named client work is separate.", "name", "alias", "client"),
	businessPattern("Internally we call %s “%s”. It's the %s work, not the similarly named job for another client.", "name", "alias", "client"),
	businessPattern("Treat “%s” and %s as one and the same — our project for %s — and keep the near-namesake client work apart.", "alias", "name", "client"),
}
var projectOwnerV13 = []frame{
	businessPattern("%s owns it internally.").frame,
	businessPattern("The internal owner is %s.").frame,
	businessPattern("%s runs it on our side.").frame,
	businessPattern("Internally it sits with %s.").frame,
}
var apRecordV13 = regexp.MustCompile(`\bAP-[A-Z0-9]+\b`)

func matchV13(patterns []businessFrame, text string) map[string]string {
	text = strings.ReplaceAll(text, ";", ",")
	for _, p := range patterns {
		if slots, ok := p.frame.match(text); ok {
			values := map[string]string{}
			for i, role := range p.roles {
				values[role] = slots[i]
			}
			return values
		}
	}
	return nil
}

// V13 varies and permutes the public identity clauses. Keep their inverse
// local to the probe; legacy benchmark parsing remains byte-for-byte unchanged.
func (s *store) ingestWorldV13(p protocol.MemoryPair) bool {
	text := strings.TrimSpace(p.Prompt)
	if len(text) > 1500 {
		return false
	}
	for _, lead := range []string{"Oh, ", "So — ", "Quick one: ", "By the way, ", "Right, ", ""} {
		if lead != "" && !strings.HasPrefix(text, lead) {
			continue
		}
		values := matchV13(personIdentityV13, strings.TrimPrefix(text, lead))
		if values != nil {
			pe := s.upsertPerson(values["name"])
			pe.nickname, pe.relation = values["nick"], values["relation"]
			return true
		}
	}
	record := apRecordV13.FindString(text)
	if record != "" && strings.Contains(text, "“") {
		// A project identity consists of 3–4 public clauses in arbitrary order.
		// Try complete sentence spans, including the one two-sentence variant.
		parts := strings.SplitAfter(text, ".")
		for i := range parts {
			for j := i + 1; j <= len(parts) && j <= i+2; j++ {
				values := matchV13(projectIdentityV13, strings.TrimSpace(strings.Join(parts[i:j], "")))
				if values == nil {
					continue
				}
				pr := s.upsertProject(values["alias"])
				pr.recordID, pr.name, pr.client = record, values["name"], values["client"]
				for _, clause := range parts {
					if _, slots, ok := matchAny(projectOwnerV13, strings.TrimSpace(clause)); ok {
						pr.leadName = slots[0]
					}
				}
				return true
			}
		}
	}
	// Existing world frames still apply, but composed v13 typo projection can
	// spend more than the legacy budget. Canonicalize only matched literals.
	banks := [][]frame{personWorkFrames, personEmailFrames, personCorrFrames, projectLedgerFrames, projectCorrFrames, tripContextFrames, tripPlanFrames, tripCorrFrames}
	for _, bank := range banks {
		for _, base := range bank {
			f := base
			f.literalBudget = 2
			if slots, ok := f.match(text); ok {
				var words []string
				n := 0
				for _, tok := range f.tokens {
					if tok.slot {
						words = append(words, tok.prefix+slots[n]+tok.suffix)
						n++
					} else {
						words = append(words, tok.literal)
					}
				}
				// Avoid recursion and preserve the actual wire pair in s.pairs.
				original := s.version
				s.version = 12
				p.Prompt = strings.Join(words, " ")
				before := len(s.pairs)
				s.ingest(p)
				s.pairs = s.pairs[:before]
				s.version = original
				return true
			}
		}
	}
	return false
}
