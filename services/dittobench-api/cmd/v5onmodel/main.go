// Command v5onmodel is the ON-MODEL winnability / grader-false-negative precheck
// for bench_version 5 (v5 plan 4.10). It answers real generated v5 memory cases
// with the LOCKED reference model (Qwen3-32B via OpenRouter) as a full-context
// honest reader, grades each with the deterministic public grader, and reports:
//
//   - the honest-reference composite and per-category means (winnability: a
//     non-degenerate 0.3-0.8 band, not ~0);
//   - a grep-parser baseline per category (can the answer be found by a literal
//     substring scan of the seeded haystack), so the honest-minus-parser GAP is
//     visible per type -- v5's whole point is a positive gap on the memory half
//     and a LARGE gap on the conversational classes;
//   - grader-false-negative candidates: cases where the model gave a plausible
//     answer but scored 0, which flags a grader bug rather than a hard case.
//
// It is a FULL-CONTEXT reader (the whole haystack is in the prompt), which is the
// right instrument for a winnability + grader audit: it isolates "can the model +
// grader handle this case" from "can retrieval find it". Production uses retrieval,
// so a real harness scores at or below this upper bound.
//
//	OPENROUTER_API_KEY=... go run ./cmd/v5onmodel --seed 424242 --run-size full
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const model = "qwen/qwen3-32b"

// systemPrompt is an honest Ditto reference reader: ground answers in stored
// memory, decline what is absent, and DO NOT dump stored facts on small talk.
const systemPrompt = `You are Ditto, a helpful personal assistant with long-term memory of this user.
Below is the user's stored memory (past conversation turns). Use it to answer the user's next message.

Rules:
- Answer ONLY from the stored memory or the user's current message. If a fact was never stated, say you don't have it — never guess a similar value.
- For a greeting or small talk ("hi", "how are you"), reply naturally and briefly. Do NOT recite stored facts that weren't asked for.
- When the user states a new fact about themselves, acknowledge it naturally (echo the value back).
- Be concise: a sentence or two. State the specific value asked for.`

type orMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type orRequest struct {
	Model       string      `json:"model"`
	Messages    []orMessage `json:"messages"`
	MaxTokens   int         `json:"max_tokens"`
	Temperature float64     `json:"temperature"`
}

func callModel(ctx context.Context, key, memory, question string) (string, error) {
	body, _ := json.Marshal(orRequest{
		Model: model,
		Messages: []orMessage{
			{Role: "system", Content: systemPrompt + "\n\n--- STORED MEMORY ---\n" + memory},
			{Role: "user", Content: question},
		},
		MaxTokens:   400,
		Temperature: 0,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var parsed struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
		Error *struct {
			Message string `json:"message"`
			Code    int    `json:"code"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return "", err
	}
	if parsed.Error != nil {
		return "", fmt.Errorf("openrouter %d: %s", parsed.Error.Code, parsed.Error.Message)
	}
	if len(parsed.Choices) == 0 {
		return "", fmt.Errorf("no choices")
	}
	return strings.TrimSpace(parsed.Choices[0].Message.Content), nil
}

// stripThink removes a leading <think>...</think> block Qwen may emit.
func stripThink(s string) string {
	if i := strings.Index(s, "</think>"); i >= 0 {
		return strings.TrimSpace(s[i+len("</think>"):])
	}
	return s
}

func main() {
	var seed int64
	var runSize string
	var maxCases int
	flag.Int64Var(&seed, "seed", 424242, "dataset seed")
	flag.StringVar(&runSize, "run-size", "full", "run size")
	flag.IntVar(&maxCases, "max-cases", 0, "cap number of cases (0 = all)")
	flag.Parse()

	key := strings.TrimSpace(os.Getenv("OPENROUTER_API_KEY"))
	if key == "" {
		log.Fatal("OPENROUTER_API_KEY not set")
	}

	prof, ok := gen.ProfileForVersion(runSize, protocol.BenchVersionV5)
	if !ok {
		log.Fatalf("unknown run_size %q", runSize)
	}
	r, err := gen.NewRNGForVersion(seed, protocol.BenchVersionV5)
	if err != nil {
		log.Fatal(err)
	}
	suite, err := gen.GenerateMemorySuiteForVersion(r, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV5)
	if err != nil {
		log.Fatal(err)
	}

	// The SEEDED haystack (what the harness is handed at /seed). This is also what
	// the grep-parser baseline scans — the parser exploit greps the seeded store,
	// NOT later /run turns.
	var hay strings.Builder
	for _, w := range suite.Waves {
		for _, p := range w.Pairs {
			hay.WriteString("- " + p.Prompt + " -> " + p.Response + "\n")
		}
	}
	parserHaystack := hay.String()

	// Model memory = seeded haystack + passive-capture simulation. A real harness
	// persists /run turns, so a no-save-verb declarative write (and a lifecycle
	// write instruction) stated in an earlier turn is in memory when a later turn
	// reads it. The write values appear nowhere in the seeded haystack, so without
	// this the write->read chains are unwinnable for THIS full-context reader (not a
	// grader bug — a harness that persists run turns has them). The parser baseline
	// does NOT get these (it only greps the seeded store), so the honest-minus-parser
	// gap on the declarative classes stays real.
	for _, sc := range suite.Cases {
		switch sc.Case.QuestionType {
		case gen.QTDeclarativeWrite, gen.QTLifecycleWrite:
			hay.WriteString("- (user stated in an earlier turn) " + sc.Case.Question + "\n")
		}
	}
	memory := hay.String()

	type catAgg struct {
		modelSum, parserSum float64
		n                   int
	}
	cats := map[string]*catAgg{}
	var order []string
	var falseNegs []string
	var modelSum, parserSum float64
	var n int

	ctx := context.Background()
	cases := suite.Cases
	if maxCases > 0 && len(cases) > maxCases {
		cases = cases[:maxCases]
	}
	fmt.Printf("on-model v5 winnability precheck: model=%s seed=%d run_size=%s cases=%d\n", model, seed, runSize, len(cases))
	fmt.Printf("haystack: %d pairs, ~%d chars\n\n", countPairs(suite), len(memory))

	for i, sc := range cases {
		mc := sc.Case
		cctx, cancel := context.WithTimeout(ctx, 90*time.Second)
		out, err := callModel(cctx, key, memory, mc.Question)
		cancel()
		if err != nil {
			log.Printf("case %d (%s): model error: %v", i, mc.QuestionType, err)
			out = ""
		}
		out = stripThink(out)
		v := grade.Memory(mc, protocol.RunResponse{FinalText: out})

		// Parser baseline: a literal substring scan of the haystack. It passes only
		// if the expected value (or an accept form) is present in the seeded pairs.
		parser := 0.0
		if parserCanGrep(mc, parserHaystack) {
			parser = 1.0
		}

		c := cats[mc.QuestionType]
		if c == nil {
			c = &catAgg{}
			cats[mc.QuestionType] = c
			order = append(order, mc.QuestionType)
		}
		c.modelSum += v.Score
		c.parserSum += parser
		c.n++
		modelSum += v.Score
		parserSum += parser
		n++

		if v.Score == 0 && looksAnswered(out) && mc.AnswerKind != protocol.AnswerDecline && mc.QuestionType != gen.QTAbstainConfab {
			falseNegs = append(falseNegs, fmt.Sprintf("[%s] Q=%q\n    expected=%q got=%q notes=%v", mc.QuestionType, trim(mc.Question, 60), mc.ExpectedAnswer, trim(out, 80), v.Notes))
		}
		fmt.Printf("%2d %-26s model=%.0f parser=%.0f  %s\n", i, mc.QuestionType, v.Score, parser, trim(strings.ReplaceAll(out, "\n", " "), 54))
	}

	fmt.Printf("\n=== per-category (model vs grep-parser) ===\n")
	sort.Strings(order)
	for _, cat := range order {
		c := cats[cat]
		fmt.Printf("%-26s model=%.2f parser=%.2f gap=%+.2f  (n=%d)\n", cat, c.modelSum/float64(c.n), c.parserSum/float64(c.n), (c.modelSum-c.parserSum)/float64(c.n), c.n)
	}
	fmt.Printf("\nOVERALL model_mean=%.3f parser_mean=%.3f gap=%+.3f (n=%d)\n", modelSum/float64(n), parserSum/float64(n), (modelSum-parserSum)/float64(n), n)
	if len(falseNegs) > 0 {
		fmt.Printf("\n=== grader-false-negative candidates (%d) — model answered but scored 0 ===\n", len(falseNegs))
		for _, f := range falseNegs {
			fmt.Println(f)
		}
	} else {
		fmt.Printf("\nNo grader-false-negative candidates flagged.\n")
	}
}

func countPairs(s gen.MemorySuite) int {
	n := 0
	for _, w := range s.Waves {
		n += len(w.Pairs)
	}
	return n
}

// parserCanGrep models the cleartext-substring exploit: can a literal scan of the
// haystack produce a passing answer. For a value/number/list case it is a grep of
// the expected value; for a decline/chitchat/acknowledge case a grep produces no
// passing answer (there is nothing in the haystack to surface), so it is 0.
func parserCanGrep(mc protocol.MemoryCase, memory string) bool {
	switch mc.AnswerKind {
	case protocol.AnswerDecline, protocol.AnswerChitchat, protocol.AnswerAcknowledge:
		return false
	}
	if mc.QuestionType == gen.QTDeclarativeAck {
		// The value is in the question, not the haystack; a haystack grep misses it.
		return false
	}
	if grade.Hit(mc.ExpectedAnswer, memory) {
		return true
	}
	for _, a := range mc.AcceptAny {
		if grade.Hit(a, memory) {
			return true
		}
	}
	for _, it := range mc.AnswerItems {
		if grade.Hit(it, memory) {
			return true
		}
	}
	return false
}

func looksAnswered(s string) bool { return len(strings.TrimSpace(s)) > 0 }

func trim(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}
