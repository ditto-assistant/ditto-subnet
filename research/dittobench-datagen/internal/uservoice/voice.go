// Package uservoice removes benchmark-like framing from V8 transcript turns.
// It only projects already-generated prose; values, instructions, and answer
// facts remain byte-for-byte within the turn.
package uservoice

import (
	"strings"
	"unicode"
)

// Render returns a natural V8 user turn without schema-like lead-ins or empty
// urgency disclaimers. Callers gate this projection to V8.
func Render(prompt string) string {
	prompt = strings.TrimSpace(prompt)
	for _, prefix := range []string{"For reference, ", "For reference: "} {
		if strings.HasPrefix(prompt, prefix) {
			prompt = capitalize(strings.TrimSpace(strings.TrimPrefix(prompt, prefix)))
			break
		}
	}
	for _, suffix := range []string{
		" Nothing urgent.", " Just keeping you posted.", " Thought I'd mention it.",
		" Just flagging it.", " No big deal.", " Figured you should know.",
	} {
		prompt = strings.TrimSuffix(prompt, suffix)
	}
	return prompt
}

func capitalize(value string) string {
	if value == "" {
		return value
	}
	runes := []rune(value)
	runes[0] = unicode.ToUpper(runes[0])
	return string(runes)
}
