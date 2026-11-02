package grade

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The v14 reply-reading vectors. Each positive is a reply a chat model writes
// for the correct value that v13 misses because of markup or sentence
// punctuation, and that miner final writers were rewriting to be graded
// (SN-342). Each negative pins a guard the relaxation must keep: a distractor
// in the same markup, a hedged reply naming both values, struck-through text,
// a number inside another number, a lone ordinal, and v13 itself.

var (
	replyDate = protocol.MemoryCase{AnswerKind: protocol.AnswerDate, ExpectedAnswer: "2026-03-04", DistractorAnswers: []string{"2026-03-05"}}
	replyBake = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Sunrise Bakery", DistractorAnswers: []string{"Moonlight Cafe"}}
	replyPIN  = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "4471", DistractorAnswers: []string{"4417"}}
	replyPct  = protocol.MemoryCase{AnswerKind: protocol.AnswerNumber, ExpectedAnswer: "15", DistractorAnswers: []string{"20"},
		Claims: []protocol.Claim{{Kind: protocol.ClaimKindQuantity, Expected: "15", Unit: "percentage points", Critical: true, Weight: 1}}}
	replySeats = protocol.MemoryCase{AnswerKind: protocol.AnswerNumber, ExpectedAnswer: "15", DistractorAnswers: []string{"20"},
		Claims: []protocol.Claim{{Kind: protocol.ClaimKindQuantity, Expected: "15", Unit: "seats", Critical: true, Weight: 1}}}
	replyCount = protocol.MemoryCase{AnswerKind: protocol.AnswerNumber, ExpectedAnswer: "3", DistractorAnswers: []string{"4"}}
	replyMoney = protocol.MemoryCase{AnswerKind: protocol.AnswerMoney, AnswerUnit: protocol.AnswerUnitMajor, AnswerCurrency: "USD", ExpectedAnswer: "120050", DistractorAnswers: []string{"150000"}}
)

func TestV14ReadsRenderedMarkdown(t *testing.T) {
	for _, tt := range []struct {
		name     string
		mc       protocol.MemoryCase
		resp     protocol.RunResponse
		v13, v14 float64
	}{
		// Positives: v13 misses, v14 credits.
		{"bold date", replyDate, protocol.RunResponse{FinalText: "Your dentist appointment is on **March 4, 2026**."}, 0, 1},
		{"italic date", replyDate, protocol.RunResponse{FinalText: "It is on *March 4, 2026*."}, 0, 1},
		{"underscore date", replyDate, protocol.RunResponse{FinalText: "It is on __March 4, 2026__."}, 0, 1},
		{"code date", replyDate, protocol.RunResponse{FinalText: "It is on `March 4, 2026`."}, 0, 1},
		{"emphasis splits a date", replyDate, protocol.RunResponse{FinalText: "It is on **March 4**, 2026."}, 0, 1},
		{"emphasis on the day", replyDate, protocol.RunResponse{FinalText: "It is on March **4**, 2026."}, 0, 1},
		{"day-month bold", replyDate, protocol.RunResponse{FinalText: "It is on **4 March 2026**."}, 0, 1},
		{"ordinal day bold", replyDate, protocol.RunResponse{FinalText: "It is on **March 4th**."}, 0, 1},
		{"labelled bullet", replyDate, protocol.RunResponse{FinalText: "- **Date:** **March 4, 2026**"}, 0, 1},
		{"blockquote bold", replyDate, protocol.RunResponse{FinalText: "> **March 4, 2026**"}, 0, 1},
		{"bold slot, plain prose", replyDate, protocol.RunResponse{Answer: "**March 4, 2026**", FinalText: "It is on March 4, 2026."}, 0, 1},
		{"plain slot, bold prose", replyDate, protocol.RunResponse{Answer: "March 4, 2026", FinalText: "It is on **March 4, 2026**."}, 0, 1},
		{"emphasis splits a name", replyBake, protocol.RunResponse{FinalText: "It was **Sunrise** Bakery."}, 0, 1},
		{"emphasis on the last word", replyBake, protocol.RunResponse{FinalText: "It was Sunrise **Bakery**."}, 0, 1},
		{"two bold words", protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "blue purple", DistractorAnswers: []string{"sea green"}},
			protocol.RunResponse{FinalText: "It is **blue** **purple**."}, 0, 1},
		{"bold amount split before cents", replyMoney, protocol.RunResponse{FinalText: "It cost **$1,200**.50 in the end."}, 0, 1},

		// Unchanged credit at both versions.
		{"plain date", replyDate, protocol.RunResponse{FinalText: "It is on March 4, 2026."}, 1, 1},
		{"heading", replyDate, protocol.RunResponse{FinalText: "### March 4, 2026"}, 1, 1},
		{"link text", replyBake, protocol.RunResponse{FinalText: "It was [Sunrise Bakery](https://maps.example/sunrise)."}, 1, 1},
		{"numbered list item", replyBake, protocol.RunResponse{FinalText: "1. It was **Sunrise Bakery**.\n2. You went on a Sunday."}, 1, 1},

		// Negatives: the guards still hold at v14.
		{"bold distractor", replyDate, protocol.RunResponse{FinalText: "It is on **March 5, 2026**."}, 0, 0},
		{"bold hedge", replyDate, protocol.RunResponse{FinalText: "It is on **March 4** or **March 5, 2026**."}, 0, 0},
		{"table names both", replyDate, protocol.RunResponse{FinalText: "| Field | Value |\n|---|---|\n| Old date | March 5, 2026 |\n| Current date | **March 4, 2026** |"}, 0, 0},
		{"split-name distractor", replyBake, protocol.RunResponse{FinalText: "It was **Moonlight** Cafe."}, 0, 0},
		{"strikethrough keeps the distractor", replyBake, protocol.RunResponse{FinalText: "It was ~~Moonlight Cafe~~ Sunrise Bakery."}, 0, 0},
		{"bold hedge on a name", replyBake, protocol.RunResponse{FinalText: "Either **Sunrise Bakery** or **Moonlight Cafe**."}, 0, 0},
		{"code span is verbatim", replyBake, protocol.RunResponse{FinalText: "It was `**Moonlight Cafe**`."}, 0, 0},
	} {
		for _, v := range []struct {
			version int
			want    float64
		}{{protocol.BenchVersionV13, tt.v13}, {protocol.BenchVersionV14, tt.v14}} {
			mc := tt.mc
			mc.BenchVersion = v.version
			if got := Memory(mc, tt.resp); got.Score != v.want {
				t.Errorf("%s at v%d: scored %v, want %v (%v)", tt.name, v.version, got.Score, v.want, got.Notes)
			}
		}
	}
}

func TestV14NumberEndsAtSentencePunctuation(t *testing.T) {
	leak := protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "4471"}
	for _, tt := range []struct {
		name     string
		mc       protocol.MemoryCase
		resp     protocol.RunResponse
		v13, v14 float64
	}{
		{"comma after a numeric value", replyPIN, protocol.RunResponse{FinalText: "Your PIN is 4471, as you saved it."}, 0, 1},
		{"bold numeric value then comma", replyPIN, protocol.RunResponse{FinalText: "Your PIN is **4471**, as you saved it."}, 1, 1},
		{"final period", replyPIN, protocol.RunResponse{FinalText: "Your PIN is 4471."}, 1, 1},
		// A forbidden value followed by punctuation is now caught.
		{"forbidden value then comma", leak, protocol.RunResponse{FinalText: "Sure, the code is 4471, right?"}, 0.5, 0},
		{"forbidden value then period", leak, protocol.RunResponse{FinalText: "Sure, the code is 4471. Anything else?"}, 0.5, 0},
		// A decimal or grouping separator still attaches.
		{"decimal tail", replyPIN, protocol.RunResponse{FinalText: "Your PIN is 4471.5"}, 0, 0},
		{"grouping tail", replyPIN, protocol.RunResponse{FinalText: "Your PIN is 4471,500"}, 0, 0},
		{"inside a longer number", replyPIN, protocol.RunResponse{FinalText: "Your PIN is 44710."}, 0, 0},
		{"negative", replyPIN, protocol.RunResponse{FinalText: "Your PIN is -4471."}, 0, 0},
		// A hedged reply naming both values is still inconsistent.
		{"comma list of value and distractor", replyPIN, protocol.RunResponse{FinalText: "Your PIN is 4471, or maybe 4417."}, 0, 0},
		{"distractor then comma", replyPIN, protocol.RunResponse{FinalText: "Your PIN is 4417, as you saved it."}, 0, 0},
		// A lone ordinal is not stripped: "1." is a number the model wrote.
		{"lone ordinal", replyCount, protocol.RunResponse{FinalText: "1. You have 3 cats."}, 0, 0},
		// A real numbered list loses its ordinals, so "3." never reads as the count.
		{"numbered list ordinals", replyCount, protocol.RunResponse{FinalText: "You have these cats:\n1. Miso\n2. Tofu\n3. Pickles\n4. Bean"}, 0, 0},
	} {
		for _, v := range []struct {
			version int
			want    float64
		}{{protocol.BenchVersionV13, tt.v13}, {protocol.BenchVersionV14, tt.v14}} {
			mc := tt.mc
			mc.BenchVersion = v.version
			if got := Memory(mc, tt.resp); got.Score != v.want {
				t.Errorf("%s at v%d: scored %v, want %v (%v)", tt.name, v.version, got.Score, v.want, got.Notes)
			}
		}
	}
}

func TestV14PercentClaimReadsPercentSign(t *testing.T) {
	for _, tt := range []struct {
		name     string
		mc       protocol.MemoryCase
		resp     protocol.RunResponse
		v13, v14 float64
	}{
		{"percent sign", replyPct, protocol.RunResponse{FinalText: "The discount is now 15%."}, 0, 1},
		{"spaced percent sign", replyPct, protocol.RunResponse{FinalText: "The discount is now 15 %."}, 0, 1},
		{"bold percent", replyPct, protocol.RunResponse{FinalText: "The discount is now **15%**."}, 0, 1},
		{"percent in slot", replyPct, protocol.RunResponse{Answer: "15%", FinalText: "The discount is now 15%."}, 0, 1},
		{"spelled percent", replyPct, protocol.RunResponse{FinalText: "The discount is now 15 percent."}, 1, 1},
		{"distractor percent", replyPct, protocol.RunResponse{FinalText: "The discount is now 20%."}, 0, 0},
		{"both percents", replyPct, protocol.RunResponse{FinalText: "The discount went from 20% to 15%."}, 0, 0},
		// A count still ignores a percentage.
		{"count ignores percent", replySeats, protocol.RunResponse{FinalText: "15% of the seats are taken."}, 0, 0},
		{"count", replySeats, protocol.RunResponse{FinalText: "There are 15 seats."}, 1, 1},
	} {
		for _, v := range []struct {
			version int
			want    float64
		}{{protocol.BenchVersionV13, tt.v13}, {protocol.BenchVersionV14, tt.v14}} {
			mc := tt.mc
			mc.BenchVersion = v.version
			if got := Memory(mc, tt.resp); got.Score != v.want {
				t.Errorf("%s at v%d: scored %v, want %v (%v)", tt.name, v.version, got.Score, v.want, got.Notes)
			}
		}
	}
}

func TestPlainReplyTextV14(t *testing.T) {
	for _, tt := range []struct{ in, want string }{
		{"Your handle is **VK-48HJXP6A63**.", "Your handle is VK-48HJXP6A63."},
		{"***both***", "both"},
		{"**_nested_**", "nested"},
		{"snake_case_name and 2*3*4", "snake_case_name and 2*3*4"},
		{"2 * 3 * 4", "2 * 3 * 4"},
		{"a ** b", "a ** b"},
		{"~~struck~~ kept", "~~struck~~ kept"},
		{"`**code**` then **text**", "**code** then text"},
		{"`` a ` b ``", "a ` b"},
		{"```\n**fenced**\n```\nafter **x**", "**fenced**\nafter x"},
		{"# Title #\n## Sub", "Title\nSub"},
		{"#1 priority", "#1 priority"},
		{"> > quoted", "quoted"},
		{"- [x] done\n* item\n+ plus\n• dot", "done\nitem\nplus\ndot"},
		{"-5 degrees", "-5 degrees"},
		{"1. one\n2. two\n   1. nested", "one\ntwo\n   nested"},
		{"5. That's the count.", "5. That's the count."},
		{"3. three\n4. four", "3. three\n4. four"},
		{"| a | b |\n|---|:-:|\n| 1 | 2 |", "a | b\n1 | 2"},
		{"See [the doc](https://x.y/(z)) and ![alt](i.png) and <https://a.b>", "See the doc and alt and https://a.b"},
		{"Title\n=====\n---\ntext", "Title\ntext"},
		{"3 418 and 3 418 and a b​c", "3 418 and 3 418 and a bc"},
		{"−5", "-5"},
		{"line next", "line\nnext"},
	} {
		if got := PlainReplyTextV14(tt.in); got != tt.want {
			t.Errorf("PlainReplyTextV14(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

// TestReplyAsReadIsVersionGated pins the exported scorer hook: unchanged below
// v14, hyphen-folded and rendered from v14.
func TestReplyAsReadIsVersionGated(t *testing.T) {
	resp := protocol.RunResponse{Answer: "**VK‑48**", FinalText: "Your handle is **VK‑48**, as saved."}
	if got := ReplyAsRead(protocol.BenchVersionV13, resp); got.Answer != resp.Answer || got.FinalText != resp.FinalText {
		t.Fatalf("v13 reply rewritten: %+v", got)
	}
	got := ReplyAsRead(protocol.BenchVersionV14, resp)
	if got.Answer != "VK-48" || got.FinalText != "Your handle is VK-48, as saved." {
		t.Fatalf("v14 reply = %+v", got)
	}
	if ReplyTextAsRead(protocol.BenchVersionV13, "**x**") != "**x**" || ReplyTextAsRead(protocol.BenchVersionV14, "**x**") != "x" {
		t.Fatal("ReplyTextAsRead is not version gated")
	}
}

// TestPlainReplyTextV14IsIdempotent keeps the rendering a fixed point on text
// without code, so a harness that already strips markdown and one that does
// not grade alike. Code content is kept verbatim, so a code span holding
// markup renders once (`**k**` reads as **k**) by design.
func TestPlainReplyTextV14IsIdempotent(t *testing.T) {
	for _, in := range []string{
		"**a** _b_ `c` [d](e) # f",
		"1. **x**\n2. y\n> z\n| p | q |\n|---|---|\n| 1 | 2 |",
		"***x*** __y__ ~~z~~ 2*3",
	} {
		once := PlainReplyTextV14(in)
		if twice := PlainReplyTextV14(once); twice != once {
			t.Errorf("not idempotent on %q: %q then %q", in, once, twice)
		}
	}
}
