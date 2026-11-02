package parserprobe

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

var quantityLeadV13 = []frame{businessPattern("Account notes for %s:", "subject").frame, businessPattern("Notes on %s's account.", "subject").frame, businessPattern("Filed for %s —", "subject").frame}
var quantityAmountV13 = regexp.MustCompile(`(?:CA\$|[€£$])[0-9]+|[0-9]+ (?:nights|seats|hours|licences|percentage points)`)
var quantityConventionV13 = []struct {
	phrase string
	effect int
}{
	{"is billed on top", 1}, {"increases", 1}, {"is added to", 1},
	{"reduces", -1}, {"comes off", -1}, {"is deducted from", -1},
	{"is a memo entry only", 0}, {"is tracked separately", 0}, {"does not change", 0},
}

func answerQuantityV13(st *store, question string) derived {
	for _, pair := range st.pairs {
		subject := ""
		words := strings.Fields(pair.Prompt)
		for n := 3; n <= 12 && n <= len(words); n++ {
			if _, slots, ok := matchAny(quantityLeadV13, strings.Join(words[:n], " ")); ok {
				subject = slots[0]
				break
			}
		}
		if subject == "" || !strings.Contains(question, subject) {
			continue
		}
		amounts := quantityAmountV13.FindAllString(pair.Prompt, -1)
		if len(amounts) != 3 {
			continue
		}
		effect, found := 0, false
		for _, c := range quantityConventionV13 {
			if containsPhraseFuzzy(pair.Prompt, c.phrase) {
				effect, found = c.effect, true
				break
			}
		}
		if !found {
			continue
		}
		nums := make([]int, 3)
		for i, amount := range amounts {
			nums[i] = atoi(reInt.FindString(amount))
		}
		amount := nums[0] + effect*nums[1] - nums[2]
		value := fmt.Sprint(amount)
		// The response must state the currency the record actually uses. Never
		// consult Claim.Unit or the grader's minor-unit representation.
		for symbol, currency := range map[string]string{"CA$": "CAD", "€": "EUR", "£": "GBP", "$": "USD"} {
			if strings.HasPrefix(amounts[0], symbol) {
				value = fmt.Sprintf("%d %s", amount, currency)
				break
			}
		}
		d := derived{family: "record-quantity", kind: protocol.AnswerValue, value: value, ok: true}
		if containsPhraseFuzzy(question, "raise lower or leave") {
			direction := map[int]string{-1: "decrease", 0: "unchanged", 1: "increase"}[effect]
			d.kind, d.items = protocol.AnswerList, []string{direction, value}
		}
		return d
	}
	return derived{}
}
